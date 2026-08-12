"""One live OCPP connection to one charge point.

Builds on ``ocpp.v16.ChargePoint``, which already provides the parts of the
OCPP-J RPC framework we would otherwise write ourselves:

* CALL / CALLRESULT / CALLERROR framing and parsing
* unique message IDs (uuid4, comfortably inside the 36 character limit)
* a call lock, so only one CALL is outstanding at a time as the spec requires
* a response timeout that raises instead of hanging forever
* CALLERROR generation for messages it cannot route

What this class adds is everything specific to us: persisting every frame in
both directions, publishing them to the dashboard, and routing inbound actions
into the domain layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call_result
from ocpp.v16.enums import Action, DataTransferStatus, RegistrationStatus

from ..bus import EventBus
from ..config import settings
from ..db.database import Database, now_db
from ..db.enums import ConnectorStatus, MessageDirection, MessageTypeId
from ..domain import events
from ..domain.authorization import authorize
from ..domain.sessions import SessionService
from ..repository import charge_points as cp_repo
from ..repository import tags as tags_repo
from ..repository import messages as messages_repo

log = logging.getLogger(__name__)


class ChargePointConnection(BaseChargePoint):
    """Handlers for everything a charger sends us."""

    def __init__(
        self,
        identity: str,
        connection: Any,
        *,
        db: Database,
        bus: EventBus,
        sessions: SessionService,
        registry: Any = None,
    ) -> None:
        super().__init__(
            identity, connection, response_timeout=settings.call_timeout_seconds
        )
        self.db = db
        self.bus = bus
        self.sessions = sessions
        self.registry = registry
        # Seconds to hold each reply, for testing a slow CSMS. Seeded from the
        # charge point's row during BootNotification (no extra query -- that
        # row is already fetched there), and refreshed on a live dashboard
        # edit via refresh_response_delay(). Never read from the database
        # anywhere else on the send path, so no outbound frame can be delayed
        # by a database read.
        self._response_delay_s = 0
        # Set just before returning from on_boot_notification, and checked (and
        # cleared) here in _send. This is the actual guarantee that the boot
        # reply is never delayed: relying on *when* _response_delay_s gets set
        # is not enough, because that assignment and this method's next call
        # are two separately scheduled tasks with no ordering between them --
        # a race that measured as a genuine ~10s delay on the boot reply
        # itself when tried.
        self._exempt_next_send = False
        # A CALLRESULT carries no action on the wire -- it is matched to its
        # request by message ID alone. Without remembering the pairing, half
        # the log reads "result 3" and tells you nothing about what answered
        # what. Entries are removed as soon as their result arrives, so this
        # only ever holds the calls currently in flight.
        self._pending_actions: dict[str, str] = {}

    # =====================================================================
    # Frame logging
    #
    # route_message and _send are the two chokepoints every frame passes
    # through, inbound and outbound. Logging here means no handler can forget
    # to do it, and CALLERRORs raised inside the library get captured too.
    # =====================================================================

    async def route_message(self, raw_msg: str) -> None:
        await self._record(raw_msg, MessageDirection.INBOUND)
        try:
            await super().route_message(raw_msg)
        except Exception:
            # The ocpp library catches this and answers InternalError with no
            # detail, which is why a real bug here looks like a network
            # problem from the charger's side and nothing shows up anywhere
            # to explain it. Logging the actual traceback is the only way to
            # ever find out what really happened -- this must never be
            # removed again, and it must never suppress the exception: the
            # library still needs it to generate the correct CALLERROR reply.
            log.exception(
                "Handler raised while processing a message from %s: %s",
                self.id, raw_msg,
            )
            raise

    async def _send(self, message: str) -> None:
        # A reply may be held to simulate a slow CSMS. Only CALLRESULT and
        # CALLERROR are delayed -- never a CALL we initiate, and never the
        # WebSocket ping/pong the library handles below this layer, so
        # keepalive is untouched. The value is a plain cached int: this method
        # does no awaiting on the database, which is what kept the boot reply
        # off the critical path.
        if self._exempt_next_send:
            self._exempt_next_send = False
        elif self._response_delay_s > 0:
            try:
                type_id = int(json.loads(message)[0])
                if type_id in (MessageTypeId.CALLRESULT, MessageTypeId.CALLERROR):
                    await asyncio.sleep(self._response_delay_s)
            except (ValueError, IndexError, TypeError):
                pass
        await self._record(message, MessageDirection.OUTBOUND)
        await super()._send(message)

    async def refresh_response_delay(self) -> None:
        """Re-read the delay after a live dashboard edit.

        The initial value comes from BootNotification; this exists only to
        pick up a change made while the connection is already open, so it
        need not wait for a reconnect. Any failure leaves the delay at its
        current value rather than breaking anything.
        """
        try:
            async with self.db.acquire() as conn:
                row = await cp_repo.get(conn, self.id)
            if row is not None:
                self._response_delay_s = int(row["response_delay_s"])
        except Exception:  # noqa: BLE001 - a test aid must never break the loop
            log.exception("Could not refresh response delay for %s", self.id)

    async def _record(self, raw: str, direction: MessageDirection) -> None:
        """Persist one frame. Never allowed to break the message loop."""
        try:
            frame = json.loads(raw)
            if not isinstance(frame, list) or not frame:
                return
            type_id = int(frame[0])
            unique_id = str(frame[1]) if len(frame) > 1 else None

            action: str | None = None
            payload: Any = None
            error_code = error_description = None
            error_details = None

            if type_id == MessageTypeId.CALL:
                action = str(frame[2]) if len(frame) > 2 else None
                payload = frame[3] if len(frame) > 3 else None
                if unique_id and action:
                    self._pending_actions[unique_id] = action
            elif type_id == MessageTypeId.CALLRESULT:
                payload = frame[2] if len(frame) > 2 else None
                action = self._pending_actions.pop(unique_id, None) if unique_id else None
            elif type_id == MessageTypeId.CALLERROR:
                error_code = str(frame[2]) if len(frame) > 2 else None
                error_description = str(frame[3]) if len(frame) > 3 else None
                error_details = frame[4] if len(frame) > 4 else None
                action = self._pending_actions.pop(unique_id, None) if unique_id else None

            async with self.db.transaction() as conn:
                await messages_repo.log(
                    conn,
                    charge_point_id=self.id,
                    direction=direction,
                    message_type_id=type_id,
                    unique_id=unique_id,
                    action=action,
                    payload=payload,
                    error_code=error_code,
                    error_description=error_description,
                    error_details=error_details,
                )
            await self.bus.publish(
                events.MESSAGE_LOGGED,
                charge_point_id=self.id,
                direction=direction.value,
                message_type_id=type_id,
                unique_id=unique_id,
                action=action,
                payload=payload,
                error_code=error_code,
            )
        except Exception:
            # A logging failure must never cost us a charger's message.
            log.exception("Failed to record frame from %s", self.id)

    # =====================================================================
    # Inbound handlers
    # =====================================================================

    @on(Action.boot_notification)
    async def on_boot_notification(self, **kwargs: Any) -> call_result.BootNotification:
        async with self.db.transaction() as conn:
            await cp_repo.apply_boot_notification(conn, self.id, kwargs)
            cp = await cp_repo.get(conn, self.id)

        interval = (
            int(cp["heartbeat_interval"]) if cp else settings.heartbeat_interval_seconds
        )
        status = (
            RegistrationStatus(cp["registration_status"])
            if cp
            else RegistrationStatus.accepted
        )
        # Applying the delay here (rather than a background task) is safe now
        # that _exempt_next_send guards the boot reply specifically -- the
        # exemption is what matters, not the timing of when this value gets
        # set.
        if cp is not None:
            self._response_delay_s = int(cp["response_delay_s"])
        log.info(
            "BootNotification from %s (%s %s, fw %s) -> %s",
            self.id,
            kwargs.get("charge_point_vendor"),
            kwargs.get("charge_point_model"),
            kwargs.get("firmware_version"),
            status.value,
        )
        await self.bus.publish(
            events.CP_BOOTED,
            charge_point_id=self.id,
            status=status.value,
            vendor=kwargs.get("charge_point_vendor"),
            model=kwargs.get("charge_point_model"),
        )

        # A charger we have just met has no sockets on record, only connector
        # 0. Some chargers volunteer a StatusNotification per connector right
        # after booting and some say nothing until something physically
        # happens -- and until then the unit is invisible on the overview.
        # Ask it directly rather than waiting. Deferred to a task because we
        # are still inside this handler and the charger is waiting on our
        # reply; only one call may be outstanding in each direction.
        asyncio.create_task(self._discover_connectors())

        # The boot reply is the one message that proves to the charger we are
        # alive at all. Delaying it -- even correctly, by configuration --
        # reads to the charger as an unresponsive CSMS and is exactly what
        # caused a boot-notification reboot loop when tried. This flag is
        # checked once by the very next _send() call, which will be this
        # reply, and then clears itself.
        self._exempt_next_send = True

        return call_result.BootNotification(
            current_time=now_db(), interval=interval, status=status
        )

    async def _discover_connectors(self) -> None:
        """Work out how many sockets this charger has, and ask their state."""
        async with self.db.acquire() as conn:
            if await cp_repo.count_physical_connectors(conn, self.id):
                return  # already known

        # Register one socket straight away. Every charger has at least one,
        # and waiting for an answer that may never come would leave the unit
        # invisible on the overview in the meantime.
        async with self.db.transaction() as conn:
            await cp_repo.ensure_connector(conn, self.id, 1)
        await self.bus.publish(events.CP_BOOTED, charge_point_id=self.id)

        count = 1
        try:
            result = await self.registry.get_configuration(
                self.id, keys=["NumberOfConnectors"]
            )
            for entry in result.get("configuration_key") or []:
                if entry.get("key") == "NumberOfConnectors":
                    count = max(1, int(entry.get("value") or 1))
        except Exception:  # noqa: BLE001 - GetConfiguration is optional in practice
            log.info(
                "%s did not answer NumberOfConnectors; assuming one socket", self.id
            )

        if count > 1:
            async with self.db.transaction() as conn:
                for connector_id in range(2, count + 1):
                    await cp_repo.ensure_connector(conn, self.id, connector_id)
            await self.bus.publish(events.CP_BOOTED, charge_point_id=self.id)
        log.info("%s registered with %s connector(s)", self.id, count)

        # Their real states are still unknown. TriggerMessage is the polite way
        # to ask; a charger without the Remote Trigger profile simply refuses,
        # and we find out the state at the first flip instead.
        for connector_id in range(1, count + 1):
            try:
                await self.registry.trigger_message(
                    self.id,
                    requested_message="StatusNotification",
                    connector_id=connector_id,
                )
            except Exception:  # noqa: BLE001
                break

    @on(Action.heartbeat)
    async def on_heartbeat(self) -> call_result.Heartbeat:
        async with self.db.transaction() as conn:
            await cp_repo.touch(conn, self.id)
        await self.bus.publish(events.CP_HEARTBEAT, charge_point_id=self.id)
        # WebSocket Ping/Pong cannot carry a timestamp, which is exactly why
        # the spec still wants a real Heartbeat: this response is the
        # charger's clock synchronisation.
        return call_result.Heartbeat(current_time=now_db())

    @on(Action.status_notification)
    async def on_status_notification(
        self, connector_id: int, error_code: str, status: str, **kwargs: Any
    ) -> call_result.StatusNotification:
        await self.sessions.handle_status_notification(
            self.id,
            connector_id,
            status,
            error_code=error_code,
            info=kwargs.get("info"),
            vendor_error_code=kwargs.get("vendor_error_code"),
        )
        return call_result.StatusNotification()

    @on(Action.authorize)
    async def on_authorize(self, id_tag: str) -> call_result.Authorize:
        """A card presented at the reader.

        Authorize carries only the card number -- OCPP does not say which
        socket it was held to. Real intent is almost always unambiguous
        anyway: whichever connector is sitting in Preparing (a cable is in,
        waiting on a card) is the one this is for. Only when that is
        genuinely ambiguous -- no connector waiting, or more than one at
        once -- does this fall back to authorizing every connector on the
        unit, the same broad behaviour used before this was narrowed down,
        since there is no way to do better with the information OCPP gives.
        """
        async with self.db.transaction() as conn:
            result = await authorize(conn, id_tag)
            if result.accepted:
                connectors = await cp_repo.list_connectors(conn, self.id)
                waiting = [
                    c for c in connectors
                    if c["connector_id"] > 0
                    and c["status"] == ConnectorStatus.PREPARING.value
                ]
                target_connector_id = (
                    waiting[0]["connector_id"] if len(waiting) == 1 else 0
                )
                await cp_repo.set_authorization(
                    conn, self.id, target_connector_id, id_tag
                )
            known = await tags_repo.get(conn, id_tag) is not None
        log.info("Authorize %s on %s -> %s", id_tag, self.id, result.status.value)

        if not known:
            # The only moment we learn a physical card exists. Blocked and
            # expired cards are already on file, so this fires solely for
            # numbers nobody has recorded yet.
            await self.bus.publish(
                events.UNKNOWN_CARD, charge_point_id=self.id, id_tag=id_tag
            )
        await self.bus.publish(
            events.CONNECTOR_STATUS,
            charge_point_id=self.id,
            authorized=result.accepted,
            id_tag=id_tag,
        )
        return call_result.Authorize(id_tag_info=result.to_id_tag_info())

    @on(Action.start_transaction)
    async def on_start_transaction(
        self,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str,
        **kwargs: Any,
    ) -> call_result.StartTransaction:
        transaction_id, id_tag_info = await self.sessions.handle_start_transaction(
            self.id,
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=meter_start,
            timestamp=timestamp,
            reservation_id=kwargs.get("reservation_id"),
        )
        return call_result.StartTransaction(
            transaction_id=transaction_id, id_tag_info=id_tag_info
        )

    @on(Action.stop_transaction)
    async def on_stop_transaction(
        self, meter_stop: int, timestamp: str, transaction_id: int, **kwargs: Any
    ) -> call_result.StopTransaction:
        id_tag_info = await self.sessions.handle_stop_transaction(
            self.id,
            transaction_id=transaction_id,
            meter_stop=meter_stop,
            timestamp=timestamp,
            reason=kwargs.get("reason"),
            id_tag=kwargs.get("id_tag"),
        )
        # idTagInfo belongs in the response only when an idTag was supplied.
        return call_result.StopTransaction(id_tag_info=id_tag_info or None)

    @on(Action.meter_values)
    async def on_meter_values(
        self, connector_id: int, meter_value: list, **kwargs: Any
    ) -> call_result.MeterValues:
        await self.sessions.handle_meter_values(
            self.id,
            connector_id=connector_id,
            meter_value=meter_value,
            transaction_id=kwargs.get("transaction_id"),
        )
        return call_result.MeterValues()

    @on(Action.data_transfer)
    async def on_data_transfer(
        self, vendor_id: str, **kwargs: Any
    ) -> call_result.DataTransfer:
        log.info("DataTransfer from %s vendor=%s", self.id, vendor_id)
        # No vendor extensions are implemented, and saying so is more useful
        # to the charger than a blanket Accepted.
        return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)

    @on(Action.firmware_status_notification)
    async def on_firmware_status(
        self, status: str
    ) -> call_result.FirmwareStatusNotification:
        log.info("Firmware status from %s: %s", self.id, status)
        return call_result.FirmwareStatusNotification()

    @on(Action.diagnostics_status_notification)
    async def on_diagnostics_status(
        self, status: str
    ) -> call_result.DiagnosticsStatusNotification:
        log.info("Diagnostics status from %s: %s", self.id, status)
        try:
            async with self.db.transaction() as conn:
                await cp_repo.set_diagnostics_status(conn, self.id, status)
        except Exception:  # noqa: BLE001 - progress display is never critical
            log.exception("Could not store diagnostics status for %s", self.id)
        return call_result.DiagnosticsStatusNotification()