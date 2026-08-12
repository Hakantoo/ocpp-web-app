"""The charging session state machine.

    [plug in]                                        [unplug / End]
        |                                                  ^
        v                                                  |
    +---------+   start   +--------+   pause   +-----------+
    | WAITING |---------->| ACTIVE |<--------->|  PAUSED   |
    +---------+           +--------+  resume   +-----------+
    Preparing             Charging             SuspendedEVSE
    no transaction        txn open             txn open, 0 W

FAULTED sits alongside ACTIVE/PAUSED rather than below them in this diagram --
it is not terminal. A charger reporting Faulted pauses the clock without
touching the transaction; real hardware frequently keeps the same
transactionId running straight through a fault window, and recovery resumes
the same session once the charger reports a real status again.

Pause installs a TxProfile with a limit of 0 W. The transaction stays open,
so the charger's cumulative meter register never resets and resuming
continues from exactly where it stopped -- there is no offset to track.

Network I/O never happens inside a database transaction. Each command reads
the state it needs in a short transaction, releases the write lock, sends the
CALL, then records the outcome in a second short transaction. Holding SQLite's
write lock for the duration of a 30 second CALL timeout would stall every
other charger on the box.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..bus import EventBus
from ..config import settings
from ..db.database import Database, now_db
from ..db.enums import (
    ChargePointErrorCode,
    ChargingProfilePurpose,
    ConnectorStatus,
    SessionState,
    StopReason,
    TransactionState,
)
from ..repository import charge_points as cp_repo
from ..repository import faults as faults_repo
from ..repository import metering as metering_repo
from ..repository import sessions as sessions_repo
from ..repository import tags as tags_repo
from ..repository import uptime as uptime_repo
from . import events
from .authorization import AuthorizationResult, authorize
from .ports import ChargePointCommands, CommandError

log = logging.getLogger(__name__)

# Statuses that mean "a cable is connected and a session should exist".
#
# Finishing is deliberately absent. It means the transaction is over but the
# cable is still in -- typically because the battery reached 100%. Opening a
# fresh session there would put the connector straight back into WAITING and
# invite a restart, when what should happen is that it sits there until the
# driver unplugs.
PLUGGED_STATUSES = {
    ConnectorStatus.PREPARING,
    ConnectorStatus.CHARGING,
    ConnectorStatus.SUSPENDED_EV,
    ConnectorStatus.SUSPENDED_EVSE,
}


class SessionError(RuntimeError):
    """The requested transition is not legal from the current state."""


@dataclass(slots=True)
class CommandResult:
    ok: bool
    session_id: int | None = None
    state: str | None = None
    detail: str = ""


class SessionService:
    def __init__(
        self, db: Database, bus: EventBus, commands: ChargePointCommands
    ) -> None:
        self.db = db
        self.bus = bus
        self.commands = commands

    # =====================================================================
    # Inbound: messages from the charge point
    # =====================================================================

    async def handle_status_notification(
        self,
        identity: str,
        connector_id: int,
        status: str,
        error_code: str = ChargePointErrorCode.NO_ERROR.value,
        info: str | None = None,
        vendor_error_code: str | None = None,
    ) -> None:
        """Drive session lifecycle from connector status.

        A cable appearing (Available -> Preparing) opens a WAITING session; a
        cable leaving (-> Available) closes whatever was open.
        """
        new_status = ConnectorStatus(status)
        session_faulted_id: int | None = None

        async with self.db.transaction() as conn:
            await cp_repo.ensure_connector(conn, identity, connector_id)
            await cp_repo.update_connector_status(
                conn, identity, connector_id, new_status, error_code, info,
                vendor_error_code,
            )
            await cp_repo.touch(conn, identity)

            # Connector 0 refers to the charge point as a whole and never
            # carries a session of its own. A fault reported against it means
            # the entire unit is down, so every socket follows -- otherwise the
            # dashboard would show Available connectors on a dead charger.
            if connector_id == 0:
                if new_status is ConnectorStatus.FAULTED:
                    affected = await cp_repo.fault_all_connectors(
                        conn, identity, error_code
                    )
                    for socket in affected:
                        existing = await faults_repo.get_open_fault(
                            conn, identity, socket
                        )
                        if existing:
                            continue
                        open_session = await sessions_repo.get_open_on_connector(
                            conn, identity, socket
                        )
                        if (
                            open_session
                            and open_session["state"] == SessionState.ACTIVE.value
                        ):
                            await sessions_repo.pause_clock(
                                conn, int(open_session["id"])
                            )
                        await faults_repo.open_fault(
                            conn,
                            charge_point_id=identity,
                            connector_id=socket,
                            error_code=error_code,
                            vendor_error_code=vendor_error_code,
                            info=info,
                            session_id=int(open_session["id"])
                            if open_session else None,
                        )
                    log.warning(
                        "%s reported a unit-level fault (%s); faulted connectors %s",
                        identity, error_code, affected,
                    )
                else:
                    # The unit-level condition cleared: close off whatever
                    # per-connector faults it opened. Sessions were never
                    # touched, so there is nothing to reopen or resume beyond
                    # the clock, which each connector's own status transition
                    # (handled below, per connector) takes care of.
                    for row in await cp_repo.list_connectors(conn, identity):
                        socket = int(row["connector_id"])
                        if socket == 0:
                            continue
                        await faults_repo.clear_open_fault(conn, identity, socket)
                await self.bus.publish(
                    events.CONNECTOR_STATUS,
                    charge_point_id=identity,
                    connector_id=connector_id,
                    status=new_status.value,
                    error_code=error_code,
                )
                return

            session = await sessions_repo.get_open_on_connector(
                conn, identity, connector_id
            )

            if new_status is ConnectorStatus.FAULTED:
                # The charger frequently keeps the transaction running straight
                # through a fault -- closing our side here just to reopen it
                # moments later is what caused the stuck-WAITING/no-Stop bug.
                # FAULTED is a real, non-terminal state: the transaction stays
                # open and the session stays open (Stop/End still work), but
                # set_state's clock coupling means moving into FAULTED stops
                # the clock exactly like moving out of ACTIVE always has.
                # WAITING is included too: it has no clock running yet, but
                # its own state field needs to track FAULTED the same way
                # ACTIVE/PAUSED already do, or the connector card (which
                # always shows Faulted) and the session page (which stayed on
                # its prior state) visibly disagree about the same event.
                if session and session["state"] in (
                    SessionState.WAITING.value,
                    SessionState.ACTIVE.value,
                    SessionState.PAUSED.value,
                ):
                    await sessions_repo.set_state(
                        conn, int(session["id"]), SessionState.FAULTED
                    )
                    session_faulted_id = int(session["id"])
                existing = await faults_repo.get_open_fault(
                    conn, identity, connector_id
                )
                if not existing:
                    await faults_repo.open_fault(
                        conn,
                        charge_point_id=identity,
                        connector_id=connector_id,
                        error_code=error_code,
                        vendor_error_code=vendor_error_code,
                        info=info,
                        session_id=int(session["id"]) if session else None,
                    )

            elif new_status in PLUGGED_STATUSES and session is None:
                # Cable in, nothing open: this is a new session waiting for a
                # Start command.
                connector_pk = await cp_repo.ensure_connector(
                    conn, identity, connector_id
                )
                session_id = await sessions_repo.create(
                    conn,
                    charge_point_id=identity,
                    connector_pk=connector_pk,
                    connector_id=connector_id,
                    state=SessionState.WAITING,
                )
                log.info(
                    "Session %s opened on %s connector %s (%s)",
                    session_id, identity, connector_id, new_status.value,
                )
                await self.bus.publish(
                    events.SESSION_CREATED,
                    session_id=session_id,
                    charge_point_id=identity,
                    connector_id=connector_id,
                    state=SessionState.WAITING.value,
                )

            elif (
                new_status is ConnectorStatus.PREPARING
                and session
                and session["state"] == SessionState.FAULTED.value
            ):
                # A fault can clear into Preparing rather than Charging --
                # nothing is being delivered, much like a fresh plug-in with
                # no transaction started yet. Whether that transaction is
                # genuinely still open, or already closed (by End, or never
                # existed at all if this session faulted straight from
                # WAITING), decides which state it belongs back in.
                #
                # started_at is not the right signal here: it is set once and
                # never cleared, so it cannot tell "a transaction is open
                # right now" apart from "one existed earlier and was already
                # closed" -- exactly the gap that let a session recover into
                # ACTIVE after End had already stopped its transaction.
                active_tx = await sessions_repo.get_active_transaction(
                    conn, int(session["id"])
                )
                if active_tx is not None:
                    await sessions_repo.set_state(
                        conn, int(session["id"]), SessionState.ACTIVE
                    )
                    await sessions_repo.pause_clock(conn, int(session["id"]))
                else:
                    await sessions_repo.set_state(
                        conn, int(session["id"]), SessionState.WAITING
                    )

            elif (
                new_status is ConnectorStatus.FINISHING
                and session
                and session["state"] == SessionState.FAULTED.value
            ):
                # A fault can clear straight into Finishing -- the
                # transaction is already closed and the cable is still in.
                # Finishing always means no transaction is open, so this
                # always recovers to WAITING, never ACTIVE.
                await sessions_repo.set_state(
                    conn, int(session["id"]), SessionState.WAITING
                )

            elif new_status is ConnectorStatus.AVAILABLE:
                # Cable out. Whoever presented a card is gone, so the next
                # driver has to present their own.
                await cp_repo.set_authorization(conn, identity, connector_id, None)
                if session is not None:
                    # The session is over regardless of which state it was
                    # in; a paused session does not survive an unplug.
                    await self._close_session(
                        conn, session, reason=StopReason.EV_DISCONNECTED
                    )

            elif new_status is ConnectorStatus.SUSPENDED_EVSE and session:
                # SuspendedEVSE means the charger is not offering energy. That
                # is what our pause looks like, but it is not only that: a
                # charger reports it for its own reasons too, such as not
                # being ready yet or a local supply limit. Only a hold we
                # actually installed counts as PAUSED -- otherwise Start finds
                # a session it thinks is paused and resumes something that was
                # never held, instead of starting it.
                if session["state"] == SessionState.ACTIVE.value:
                    ours = await sessions_repo.get_active_pause_profile(
                        conn, identity, connector_id
                    )
                    if ours:
                        await sessions_repo.set_state(
                            conn, int(session["id"]), SessionState.PAUSED
                        )
                    else:
                        # The charger's own decision. The session is still
                        # running, but no energy is moving.
                        await sessions_repo.pause_clock(conn, int(session["id"]))
                elif session["state"] == SessionState.FAULTED.value:
                    # A fault clearing straight into SuspendedEVSE rather than
                    # Charging: still not delivering, so this is not "back to
                    # normal" in the way Charging would be, but the fault
                    # itself is over. Whether that means PAUSED or ACTIVE
                    # depends on whether we are the reason nothing is
                    # flowing (a hold we installed ourselves) or the charger
                    # is, the same distinction the ACTIVE branch above
                    # already makes -- a fault clearing does not lift a hold
                    # nobody asked to lift, and recovering to ACTIVE here
                    # would show Stop on the dashboard where it should show
                    # Resume.
                    active_tx = await sessions_repo.get_active_transaction(
                        conn, int(session["id"])
                    )
                    if active_tx is not None:
                        ours = await sessions_repo.get_active_pause_profile(
                            conn, identity, connector_id
                        )
                        if ours:
                            await sessions_repo.set_state(
                                conn, int(session["id"]), SessionState.PAUSED
                            )
                        else:
                            await sessions_repo.set_state(
                                conn, int(session["id"]), SessionState.ACTIVE
                            )
                            await sessions_repo.pause_clock(conn, int(session["id"]))
                    else:
                        await sessions_repo.set_state(
                            conn, int(session["id"]), SessionState.WAITING
                        )

            elif new_status is ConnectorStatus.SUSPENDED_EV and session:
                # The *car* stopped drawing -- typically because it is full.
                # We did not pause anything, so the session stays ACTIVE and
                # the transaction stays open. But no energy is moving, and
                # charging time is meant to measure delivery, so the clock
                # stops here and restarts if the car resumes.
                if session["state"] == SessionState.ACTIVE.value:
                    await sessions_repo.pause_clock(conn, int(session["id"]))
                elif session["state"] == SessionState.FAULTED.value:
                    # Fault cleared, but the car is not drawing either way --
                    # recover into ACTIVE with the clock left stopped, unless
                    # no transaction is genuinely open right now (either this
                    # session faulted straight from WAITING and never had
                    # one, or it did and End already closed it).
                    active_tx = await sessions_repo.get_active_transaction(
                        conn, int(session["id"])
                    )
                    if active_tx is not None:
                        await sessions_repo.set_state(
                            conn, int(session["id"]), SessionState.ACTIVE
                        )
                        await sessions_repo.pause_clock(conn, int(session["id"]))
                    else:
                        await sessions_repo.set_state(
                            conn, int(session["id"]), SessionState.WAITING
                        )

            elif new_status is ConnectorStatus.CHARGING and session:
                if session["state"] == SessionState.PAUSED.value:
                    await sessions_repo.set_state(
                        conn, int(session["id"]), SessionState.ACTIVE
                    )
                elif session["state"] == SessionState.FAULTED.value:
                    # The fault cleared and the charger is delivering again:
                    # recover into ACTIVE, which starts the clock as a side
                    # effect of the state change itself.
                    await sessions_repo.set_state(
                        conn, int(session["id"]), SessionState.ACTIVE
                    )
                elif session["state"] == SessionState.ACTIVE.value:
                    # Coming back from SuspendedEV: resume the clock.
                    await sessions_repo.resume_clock(conn, int(session["id"]))

            if new_status is not ConnectorStatus.FAULTED:
                # Whatever the new status is, the fault (if any) is over.
                # Independent of the branches above, since a fault can clear
                # into any status -- Preparing, Charging, even straight to
                # Available if the cable came out during the fault.
                await faults_repo.clear_open_fault(conn, identity, connector_id)

        await self.bus.publish(
            events.CONNECTOR_STATUS,
            charge_point_id=identity,
            connector_id=connector_id,
            status=new_status.value,
            error_code=error_code,
        )
        if session_faulted_id is not None:
            await self.bus.publish(
                events.SESSION_FAULTED,
                session_id=session_faulted_id,
                charge_point_id=identity,
                connector_id=connector_id,
            )

    async def handle_start_transaction(
        self,
        identity: str,
        *,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str | None = None,
        reservation_id: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Open a transaction. Returns (transaction_id, id_tag_info)."""
        async with self.db.transaction() as conn:
            auth = await authorize(conn, id_tag, allow_concurrent=True)
            connector_pk = await cp_repo.ensure_connector(conn, identity, connector_id)

            session = await sessions_repo.get_open_on_connector(
                conn, identity, connector_id
            )
            if session is None:
                # The charger started without ever reporting Preparing. Create
                # the session now rather than dropping the transaction.
                session_id = await sessions_repo.create(
                    conn,
                    charge_point_id=identity,
                    connector_pk=connector_pk,
                    connector_id=connector_id,
                    id_tag=id_tag,
                    state=SessionState.WAITING,
                )
            else:
                session_id = int(session["id"])

            if not auth.accepted:
                # Still allocate an ID: the charger needs one to reference in
                # StopTransaction, and it will stop on its own.
                tx_id = await sessions_repo.next_transaction_id(conn)
                log.warning(
                    "StartTransaction on %s/%s refused for tag %s (%s)",
                    identity, connector_id, id_tag, auth.status.value,
                )
                return tx_id, auth.to_id_tag_info()

            tx_id = await sessions_repo.next_transaction_id(conn)
            await sessions_repo.create_transaction(
                conn,
                ocpp_transaction_id=tx_id,
                session_id=session_id,
                charge_point_id=identity,
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start_wh=int(meter_start),
                started_at=timestamp,
                reservation_id=reservation_id,
            )
            # The vehicle was bound when the cable went in, so only the card
            # needs recording here. The clock starts only if the connector
            # is genuinely Charging right now -- a card swipe can open the
            # transaction while still at SuspendedEV, with the real Charging
            # status notification following as its own, separate message.
            connector_row = await cp_repo.get_connector(conn, identity, connector_id)
            currently_charging = bool(
                connector_row and connector_row["status"] == ConnectorStatus.CHARGING.value
            )
            await sessions_repo.mark_started(
                conn, session_id, id_tag=id_tag, currently_charging=currently_charging
            )
            # Some chargers never send Authorize -- they validate the card
            # themselves and only mention it here. Recording it means the
            # connector shows who is charging either way.
            await cp_repo.set_authorization(conn, identity, connector_id, id_tag)
            log.info(
                "Transaction %s started on %s/%s tag=%s meterStart=%s",
                tx_id, identity, connector_id, id_tag, meter_start,
            )

        await self.bus.publish(
            events.SESSION_STARTED,
            session_id=session_id,
            charge_point_id=identity,
            connector_id=connector_id,
            transaction_id=tx_id,
            id_tag=id_tag,
            state=SessionState.ACTIVE.value,
        )
        return tx_id, auth.to_id_tag_info()

    async def handle_stop_transaction(
        self,
        identity: str,
        *,
        transaction_id: int,
        meter_stop: int,
        timestamp: str | None = None,
        reason: str | None = None,
        id_tag: str | None = None,
    ) -> dict[str, Any]:
        """Close a transaction. The session stays open for another Start —
        it only ends when the cable comes out."""
        id_tag_info: dict[str, Any] = {}
        session_id: int | None = None

        async with self.db.transaction() as conn:
            if id_tag:
                auth = await authorize(conn, id_tag, allow_concurrent=True)
                id_tag_info = auth.to_id_tag_info()

            tx = await sessions_repo.get_transaction_by_ocpp_id(conn, transaction_id)
            if tx is None:
                log.warning(
                    "StopTransaction for unknown transaction %s from %s",
                    transaction_id, identity,
                )
                return id_tag_info

            session_id = int(tx["session_id"])

            # A duplicate StopTransaction can arrive for a transaction we have
            # already closed (e.g. two race-condition retries from the
            # charger). Acknowledge it -- the charger needs a reply, or it
            # treats the exchange as failed and reboots -- but do not process
            # it twice. FAULTED is not terminal: the transaction is very
            # likely still genuinely open, and this StopTransaction is the
            # charger legitimately closing it, which must go through below.
            already_stopped = (
                str(tx.get("state")) != TransactionState.ACTIVE.value
            )
            session_row = await sessions_repo.get(conn, session_id)
            terminal = (
                session_row is not None
                and session_row["state"] == SessionState.COMPLETED.value
            )
            if already_stopped or terminal:
                log.info(
                    "StopTransaction %s on %s already settled (tx stopped=%s, "
                    "session terminal=%s); acknowledging without changes",
                    transaction_id, identity, already_stopped, terminal,
                )
                return id_tag_info
            await sessions_repo.stop_transaction(
                conn,
                int(tx["id"]),
                meter_stop_wh=int(meter_stop),
                reason=reason or StopReason.LOCAL.value,
                stopped_at=timestamp,
            )
            energy = await sessions_repo.refresh_energy(conn, session_id)
            # The session stays open — WAITING for the next Start, unless it
            # is FAULTED. A transaction closing (whether the charger stopped
            # it on its own, or our own End command asked it to) says
            # nothing about whether the underlying fault condition is
            # actually resolved -- only a real StatusNotification reporting
            # something other than Faulted means that. Leaving the session
            # FAULTED here is what stops End from silently implying "ready
            # to go" on a connector that is still genuinely faulted.
            if session_row is None or session_row["state"] != SessionState.FAULTED.value:
                await sessions_repo.set_state(
                    conn, session_id, SessionState.WAITING
                )
            await sessions_repo.clear_profiles_for_connector(
                conn, identity, int(tx["connector_id"])
            )
            await self._apply_energy_to_vehicle(conn, session_id)
            log.info(
                "Transaction %s stopped on %s meterStop=%s reason=%s energy=%s Wh",
                transaction_id, identity, meter_stop, reason, energy,
            )

        await self.bus.publish(
            events.SESSION_ENDED,
            session_id=session_id,
            charge_point_id=identity,
            transaction_id=transaction_id,
            reason=reason,
            state=SessionState.COMPLETED.value,
        )
        return id_tag_info

    async def handle_meter_values(
        self,
        identity: str,
        *,
        connector_id: int,
        meter_value: list[dict[str, Any]],
        transaction_id: int | None = None,
    ) -> None:
        """Ingest sampled values and advance the live energy total."""
        from ..db.enums import Measurand

        stored = 0
        energy_wh: int | None = None
        soc: float | None = None
        power_w: float | None = None
        session_id: int | None = None

        async with self.db.transaction() as conn:
            tx = (
                await sessions_repo.get_transaction_by_ocpp_id(conn, transaction_id)
                if transaction_id is not None
                else None
            )
            if tx is None:
                session = await sessions_repo.get_open_on_connector(
                    conn, identity, connector_id
                )
                if session:
                    session_id = int(session["id"])
                    tx = await sessions_repo.get_active_transaction(conn, session_id)
            else:
                session_id = int(tx["session_id"])

            samples: list[dict[str, Any]] = []
            for entry in meter_value:
                ts = entry.get("timestamp") or now_db()
                for sv in entry.get("sampled_value", []):
                    measurand = sv.get("measurand") or (
                        Measurand.ENERGY_ACTIVE_IMPORT_REGISTER.value
                    )
                    try:
                        value = float(sv["value"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    samples.append(
                        {
                            "transaction_id": int(tx["id"]) if tx else None,
                            "session_id": session_id,
                            "charge_point_id": identity,
                            "connector_id": connector_id,
                            "timestamp": ts,
                            "measurand": measurand,
                            "value": value,
                            "unit": sv.get("unit"),
                            "phase": sv.get("phase"),
                            "context": sv.get("context"),
                        }
                    )
                    if measurand == Measurand.ENERGY_ACTIVE_IMPORT_REGISTER.value:
                        # kWh is legal on the wire; normalise to Wh.
                        energy_wh = int(
                            value * 1000 if sv.get("unit") == "kWh" else value
                        )
                    elif measurand == Measurand.SOC.value:
                        soc = value
                    elif measurand == Measurand.POWER_ACTIVE_IMPORT.value:
                        power_w = value * 1000 if sv.get("unit") == "kW" else value

            stored = await metering_repo.insert_samples(conn, samples)

            if tx is not None and energy_wh is not None:
                await sessions_repo.update_meter_last(conn, int(tx["id"]), energy_wh)
            if session_id is not None:
                total = await sessions_repo.refresh_energy(conn, session_id)
                session = await sessions_repo.get(conn, session_id)
                if soc is not None and session and session["vehicle_id"]:
                    await tags_repo.set_soc(conn, int(session["vehicle_id"]), soc)
            else:
                total = 0
            await cp_repo.touch(conn, identity)

        if stored:
            await self.bus.publish(
                events.METER_VALUES,
                charge_point_id=identity,
                connector_id=connector_id,
                session_id=session_id,
                transaction_id=transaction_id,
                energy_wh=total if session_id else None,
                register_wh=energy_wh,
                power_w=power_w,
                soc=soc,
                samples=stored,
            )

    # =====================================================================
    # Outbound: commands from the dashboard or the scheduler
    # =====================================================================

    async def start(
        self, identity: str, connector_id: int, id_tag: str | None = None
    ) -> CommandResult:
        """WAITING -> ACTIVE, or PAUSED -> ACTIVE.

        Pressing Start on a paused session resumes it rather than opening a
        second transaction, which is what a user means by the button.
        """
        async with self.db.transaction() as conn:
            session = await sessions_repo.get_open_on_connector(
                conn, identity, connector_id
            )
            connector = await cp_repo.get_connector(conn, identity, connector_id)
            conn_status = connector["status"] if connector else None

            # A faulted connector rejects every command, and a rejected
            # RemoteStartTransaction can feed the charger's own reconnect
            # cycle. Refuse here so nothing rejectable is ever sent -- the
            # charger clears the fault on its own and Start works again after.
            if conn_status == ConnectorStatus.FAULTED.value:
                raise SessionError(
                    f"{identity} connector {connector_id} is faulted; "
                    "wait for it to clear before starting"
                )

            if (
                session is not None
                and session["state"] == SessionState.ACTIVE.value
                and conn_status == ConnectorStatus.CHARGING.value
            ):
                return CommandResult(
                    True, int(session["id"]), SessionState.ACTIVE.value,
                    "Already charging",
                )

            # ACTIVE but the connector is suspended: a transaction is open yet
            # nothing is flowing. Start has to do something about that rather
            # than report success and send nothing.
            nudge = (
                session is not None
                and session["state"] == SessionState.ACTIVE.value
            )

            presented = await cp_repo.get_authorization(conn, identity, connector_id)
            cp = await cp_repo.get(conn, identity)

            # Only chargers that actually report card reads can be gated on
            # one. A charger that handles cards internally never sends
            # Authorize, and requiring it there would block Start forever with
            # nothing on the wire to explain why.
            if cp and cp["require_card_before_start"] and not presented:
                raise SessionError(
                    "Present an RFID card at the charger before starting"
                )

            tag = (
                id_tag
                or presented
                or (session["id_tag"] if session else None)
                or await self._default_tag(conn)
            )
            if not tag:
                raise SessionError(
                    "No RFID card available. Add one under Tags & cars."
                )
            auth = await authorize(conn, tag, allow_concurrent=True)
            if not auth.accepted:
                raise SessionError(f"Card {tag} is not authorised ({auth.status.value})")

        if session is not None and session["state"] == SessionState.PAUSED.value:
            return await self.resume(int(session["id"]))

        if nudge:
            # Lift any limit that may be holding it, then ask the charger what
            # it is actually doing so the dashboard shows the truth.
            await self.commands.clear_charging_profile(
                identity, connector_id=connector_id
            )
            async with self.db.transaction() as conn:
                await sessions_repo.clear_profiles_for_connector(
                    conn, identity, connector_id
                )
            return CommandResult(
                True, int(session["id"]), SessionState.ACTIVE.value, "Resumed"
            )

        # No open session, or one merely waiting. Either way, ask the charger
        # rather than deciding for it whether a cable is present: our view of
        # the connector can be stale or simply wrong, and a charger that has
        # nothing plugged in answers Rejected, which is the truth and worth
        # showing. Refusing here put nothing on the wire at all.
        result = await self.commands.remote_start_transaction(
            identity, id_tag=tag, connector_id=connector_id
        )
        if result.get("status") != "Accepted":
            raise CommandError(
                f"RemoteStartTransaction rejected by {identity}: {result.get('status')}"
            )


        # The session becomes ACTIVE when StartTransaction arrives, not here,
        # and is created there too if this connector had none.
        return CommandResult(
            True,
            int(session["id"]) if session else None,
            session["state"] if session else None,
            "Remote start accepted",
        )

    async def pause(self, session_id: int, limit: float = 0.0) -> CommandResult:
        """Cap delivery. `limit` is in amps; 0 means stop entirely."""
        """ACTIVE -> PAUSED by installing a 0 W TxProfile.

        The transaction stays open. The charger reports SuspendedEVSE and its
        energy register stops advancing, so resuming needs no bookkeeping.
        """
        async with self.db.transaction() as conn:
            session = await sessions_repo.get(conn, session_id)
            if session is None:
                raise SessionError(f"No such session {session_id}")
            if session["state"] == SessionState.FAULTED.value:
                raise SessionError(
                    f"Session {session_id} is faulted; wait for it to clear "
                    "before pausing"
                )
            if session["state"] != SessionState.ACTIVE.value:
                raise SessionError(
                    f"Session {session_id} is {session['state']}, not ACTIVE"
                )
            tx = await sessions_repo.get_active_transaction(conn, session_id)
            if tx is None:
                raise SessionError(f"Session {session_id} has no open transaction")
            cp = await cp_repo.get(conn, session["charge_point_id"])
            identity = session["charge_point_id"]
            connector_id = int(session["connector_id"])
            supports_profiles = bool(cp and cp["supports_charging_profiles"])
            ocpp_tx_id = int(tx["ocpp_transaction_id"])
            limits = await cp_repo.charging_profile_limits(conn, identity)

        if not supports_profiles:
            # Fallback for hardware that rejects SetChargingProfile: end the
            # transaction. The session model already spans several, so the
            # dashboard total survives.
            log.info("%s has no profile support; pausing by stopping txn", identity)
            return await self.end(session_id, reason=StopReason.LOCAL)

        profile_id = settings.pause_profile_id_base + connector_id

        # Try the combinations the charger is most likely to accept, starting
        # with whatever it has told us about itself. A rejection carries no
        # reason in OCPP 1.6 -- just "Rejected" -- so the only way to find the
        # dialect a charger speaks is to offer each one.
        attempts: list[tuple[str, int]] = []
        preferred_unit = limits.get("unit")
        max_stack = limits.get("max_stack_level")
        stack = (
            min(settings.pause_profile_stack_level, max_stack)
            if max_stack is not None
            else settings.pause_profile_stack_level
        )
        for unit in ([preferred_unit] if preferred_unit else ["A", "W"]):
            attempts.append((unit, stack))
        if max_stack is None:
            # Never asked the charger: a lower stack level is the usual reason
            # a profile is refused, so offer level 0 as well.
            attempts += [(unit, 0) for unit, _ in list(attempts)]

        result: dict[str, Any] = {}
        for unit, level in attempts:
            result = await self.commands.set_charging_profile(
                identity,
                connector_id=connector_id,
                # The caller works in amps because that is what an AC socket
                # is rated in. A charger that only accepts watts needs the
                # same limit expressed its way.
                cs_charging_profiles=_limit_profile(
                    profile_id, ocpp_tx_id, unit=unit, stack_level=level,
                    limit=limit if unit == "A" else limit * 230.0,
                ),
            )
            if result.get("status") == "Accepted":
                accepted_unit, accepted_level = unit, level
                break
            log.info(
                "%s refused a %s %s profile at stack level %s",
                identity, limit, unit, level,
            )
        else:
            raise CommandError(
                f"{identity} rejected every charging profile we offered. "
                f"Run GetConfiguration on it to see what it supports."
            )

        async with self.db.transaction() as conn:
            await sessions_repo.record_profile(
                conn,
                charge_point_id=identity,
                connector_id=connector_id,
                session_id=session_id,
                ocpp_profile_id=profile_id,
                purpose=ChargingProfilePurpose.TX_PROFILE.value,
                stack_level=accepted_level,
                # Recorded in amps whatever the wire used, so the dashboard
                # never has to guess which unit a row is in.
                limit_value=float(limit),
                limit_unit="A",
            )
            # Only a limit of zero is a pause. Anything above it is still
            # charging, just more slowly, so the session stays ACTIVE.
            if limit <= 0:
                await sessions_repo.set_state(conn, session_id, SessionState.PAUSED)

        log.info(
            "Session %s limited to %s %s (profile %s, stack level %s)",
            session_id, limit, accepted_unit, profile_id, accepted_level,
        )
        state = (
            SessionState.PAUSED.value if limit <= 0 else SessionState.ACTIVE.value
        )
        await self.bus.publish(
            events.SESSION_PAUSED,
            session_id=session_id,
            charge_point_id=identity,
            connector_id=connector_id,
            state=state,
        )
        return CommandResult(
            True,
            session_id,
            state,
            "Paused" if limit <= 0 else f"Limited to {limit:g} A",
        )

    async def resume(self, session_id: int) -> CommandResult:
        """PAUSED -> ACTIVE by clearing the 0 W profile."""
        async with self.db.transaction() as conn:
            session = await sessions_repo.get(conn, session_id)
            if session is None:
                raise SessionError(f"No such session {session_id}")
            if session["state"] != SessionState.PAUSED.value:
                raise SessionError(
                    f"Session {session_id} is {session['state']}, not PAUSED"
                )
            identity = session["charge_point_id"]
            connector_id = int(session["connector_id"])
            profile = await sessions_repo.get_active_pause_profile(
                conn, identity, connector_id
            )

        # No purpose filter: a limit installed as TxDefaultProfile or
        # ChargePointMaxProfile holds the connector down just as effectively,
        # and clearing only TxProfile would leave it in place.
        result = await self.commands.clear_charging_profile(
            identity,
            profile_id=int(profile["ocpp_profile_id"]) if profile else None,
            connector_id=connector_id,
        )
        # "Unknown" means there was no such profile installed, which is the
        # state we wanted anyway. Only a hard failure is worth aborting on.
        if result.get("status") not in ("Accepted", "Unknown"):
            raise CommandError(
                f"ClearChargingProfile rejected by {identity}: {result.get('status')}"
            )

        async with self.db.transaction() as conn:
            if profile:
                await sessions_repo.clear_profile(conn, int(profile["id"]))
            else:
                await sessions_repo.clear_profiles_for_connector(
                    conn, identity, connector_id
                )
            # set_state alone would unconditionally start the clock the
            # instant the pause lifts -- but clearing the profile is not the
            # same fact as power actually flowing again. The charger reports
            # Charging as its own separate StatusNotification a moment
            # later; resuming quickly enough can race ahead of it, starting
            # the clock before delivery has genuinely resumed. Checking the
            # connector's real, current status here closes that race the
            # same way a fresh Start already does.
            await sessions_repo.set_state(
                conn, session_id, SessionState.ACTIVE, start_clock=False
            )
            connector = await cp_repo.get_connector(conn, identity, connector_id)
            if connector and connector["status"] == ConnectorStatus.CHARGING.value:
                await sessions_repo.resume_clock(conn, session_id)

        log.info("Session %s resumed", session_id)


        await self.bus.publish(
            events.SESSION_RESUMED,
            session_id=session_id,
            charge_point_id=identity,
            connector_id=connector_id,
            state=SessionState.ACTIVE.value,
        )
        return CommandResult(True, session_id, SessionState.ACTIVE.value, "Resumed")

    async def end(
        self, session_id: int, reason: StopReason = StopReason.REMOTE
    ) -> CommandResult:
        """Close the current transaction. The session stays open so the next
        Start can attach a new transaction to it without a second card tap."""
        async with self.db.transaction() as conn:
            session = await sessions_repo.get(conn, session_id)
            if session is None:
                raise SessionError(f"No such session {session_id}")
            if session["state"] == SessionState.COMPLETED.value:
                return CommandResult(
                    True, session_id, session["state"], "Already ended"
                )
            if session["state"] == SessionState.FAULTED.value:
                raise SessionError(
                    f"Session {session_id} is faulted; wait for it to clear "
                    "before ending"
                )
            identity = session["charge_point_id"]
            connector_id = int(session["connector_id"])
            tx = await sessions_repo.get_active_transaction(conn, session_id)
            ocpp_tx_id = int(tx["ocpp_transaction_id"]) if tx else None

        if ocpp_tx_id is not None and self.commands.is_connected(identity):
            result = await self.commands.remote_stop_transaction(
                identity, transaction_id=ocpp_tx_id
            )
            if result.get("status") != "Accepted":
                raise CommandError(
                    f"RemoteStopTransaction rejected by {identity}: "
                    f"{result.get('status')}"
                )
            # StopTransaction will arrive and set the session back to WAITING.
            return CommandResult(
                True, session_id, session["state"], "Remote stop accepted"
            )

        # No open transaction, or the charger is offline: just put the
        # session back to WAITING so the next Start can use it.
        async with self.db.transaction() as conn:
            session = await sessions_repo.get(conn, session_id)
            if session and session["state"] != SessionState.COMPLETED.value:
                await sessions_repo.set_state(conn, session_id, SessionState.WAITING)

        return CommandResult(
            True, session_id, SessionState.WAITING.value, "Transaction ended"
        )

    # =====================================================================
    # Vehicle binding
    #
    # OCPP 1.6 has no vehicle identity, so the charger tells us directly over
    # a side channel when a car is connected. That is a lab convenience: real
    # hardware cannot do this, and with a physical charger a session simply
    # has no vehicle. The benefit is that a session knows its car from the
    # moment the cable goes in, rather than waiting for a card.
    # =====================================================================

    async def attach_vehicle(
        self, identity: str, connector_id: int, vehicle_id: int
    ) -> int:
        """Bind a car to whatever session is open on this connector."""
        async with self.db.transaction() as conn:
            vehicle = await tags_repo.get_vehicle(conn, vehicle_id)
            if vehicle is None:
                raise SessionError(f"No vehicle {vehicle_id}")

            session = await sessions_repo.get_open_on_connector(
                conn, identity, connector_id
            )
            elsewhere = await tags_repo.is_plugged_in(conn, vehicle_id)
            if elsewhere and (session is None or int(elsewhere["id"]) != int(session["id"])):
                raise SessionError(
                    f"{vehicle['name']} is already plugged into "
                    f"{elsewhere['charge_point_id']} connector "
                    f"{elsewhere['connector_id']}"
                )

            if session is None:
                # The status notification has not landed yet; create the
                # session now so the binding has somewhere to go.
                connector_pk = await cp_repo.ensure_connector(
                    conn, identity, connector_id
                )
                session_id = await sessions_repo.create(
                    conn,
                    charge_point_id=identity,
                    connector_pk=connector_pk,
                    connector_id=connector_id,
                    vehicle_id=vehicle_id,
                )
            else:
                session_id = int(session["id"])
                await sessions_repo.set_vehicle(conn, session_id, vehicle_id)

        log.info(
            "Vehicle %s attached to %s connector %s (session %s)",
            vehicle_id, identity, connector_id, session_id,
        )
        await self.bus.publish(
            events.SESSION_UPDATED,
            session_id=session_id,
            charge_point_id=identity,
            connector_id=connector_id,
            vehicle_id=vehicle_id,
        )
        return session_id

    # =====================================================================
    # Connectivity
    # =====================================================================

    async def on_charge_point_connected(self, identity: str) -> None:
        async with self.db.transaction() as conn:
            await cp_repo.set_online(conn, identity, True)
            await uptime_repo.record(conn, identity, "connected")
        await self.bus.publish(events.CP_CONNECTED, charge_point_id=identity)

    async def on_charge_point_disconnected(self, identity: str) -> None:
        """A dropped WebSocket does not stop the car charging.

        Open sessions are deliberately left open: the charger keeps going
        autonomously and will report what happened when it reconnects.
        Connector states are marked Unavailable because we genuinely no longer
        know them, and showing a stale Charging badge would be a lie.
        """
        async with self.db.transaction() as conn:
            await cp_repo.set_online(conn, identity, False)
            await cp_repo.mark_all_connectors_unavailable(conn, identity)
            await uptime_repo.record(conn, identity, "disconnected")
        await self.bus.publish(events.CP_DISCONNECTED, charge_point_id=identity)

    # =====================================================================
    # Helpers
    # =====================================================================

    async def _release_vehicle_on_close(self, conn: Any, session_id: int) -> None:
        """Nothing to do: the session keeps its vehicle for the history, and
        the partial unique index only counts open sessions, so closing one
        frees the car automatically."""

    async def _close_session(
        self,
        conn: Any,
        session: dict[str, Any],
        *,
        reason: StopReason,
    ) -> None:
        session_id = int(session["id"])
        await sessions_repo.stop_active_transaction(conn, session_id, reason=reason)
        await sessions_repo.refresh_energy(conn, session_id)
        await sessions_repo.mark_completed(conn, session_id, reason=reason)
        await sessions_repo.clear_profiles_for_connector(
            conn, session["charge_point_id"], int(session["connector_id"])
        )
        await self._apply_energy_to_vehicle(conn, session_id)
        log.info("Session %s completed (%s)", session_id, reason.value)

    async def _apply_energy_to_vehicle(self, conn: Any, session_id: int) -> None:
        """Credit delivered energy to the car's stored state of charge.

        Only used when the charger never reported an SoC measurand; a real
        reading always wins over this estimate.
        """
        session = await sessions_repo.get(conn, session_id)
        if not session or not session["vehicle_id"]:
            return
        latest_soc = await metering_repo.latest(conn, session_id, "SoC")
        if latest_soc:
            await tags_repo.set_soc(
                conn, int(session["vehicle_id"]), float(latest_soc["value"])
            )
            return
        vehicle = await tags_repo.get_vehicle(conn, int(session["vehicle_id"]))
        if not vehicle:
            return
        added = (int(session["energy_wh"]) / 1000.0) / float(
            vehicle["battery_capacity_kwh"]
        ) * 100.0
        await tags_repo.set_soc(
            conn, int(vehicle["id"]), float(vehicle["current_soc"]) + added
        )

    async def _default_tag(self, conn: Any) -> str | None:
        """Any usable card.

        Used when Start is pressed on a session where no card has been
        presented, which is the normal case here: the dashboard is the
        operator, so there is no physical card to swipe.
        """
        async with conn.execute(
            """
            SELECT id_tag FROM id_tags
             WHERE status = 'Accepted'
               AND (expiry_date IS NULL OR expiry_date > ?)
             ORDER BY id_tag LIMIT 1
            """,
            (now_db(),),
        ) as cur:
            row = await cur.fetchone()
        return row["id_tag"] if row else None


def _limit_profile(
    profile_id: int,
    transaction_id: int,
    *,
    unit: str,
    stack_level: int,
    limit: float,
) -> dict[str, Any]:
    """A TxProfile capping delivery at `limit`.

    Pausing and throttling are the same message with a different number: zero
    means deliver nothing, six amps means deliver slowly. Which *unit* a
    charger will accept is the awkward part -- AC units commonly support amps
    only and reject watts outright -- so it comes from what the charger
    reports rather than being assumed.
    """
    return {
        "charging_profile_id": profile_id,
        "transaction_id": transaction_id,
        "stack_level": stack_level,
        "charging_profile_purpose": ChargingProfilePurpose.TX_PROFILE.value,
        "charging_profile_kind": "Relative",
        "charging_schedule": {
            "charging_rate_unit": unit,
            "charging_schedule_period": [
                {"start_period": 0, "limit": float(limit)}
            ],
        },
    }