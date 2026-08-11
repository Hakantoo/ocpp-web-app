"""A simulated charge point that speaks real OCPP 1.6J.

This is a genuine OCPP client, not a mock: it opens a WebSocket to the CSMS
and exchanges the same frames real hardware would, so it exercises exactly the
code path a physical charger will.

The behaviour that matters most:

* A cumulative energy register that only ever moves forward. Pausing stops it
  advancing; it does not reset. That is what makes resume-from-where-you-left
  -off work with no offset bookkeeping anywhere.
* SetChargingProfile with a 0 W limit puts the connector into SuspendedEVSE
  and holds the register still, with the transaction still open.
* ClearChargingProfile lifts the limit and charging continues from the same
  register value.
* A full battery ends the transaction and parks the connector at Finishing
  until the cable is removed, rather than dropping back to a state that would
  invite an immediate restart.
* Two independent things have to be true at once for current to flow: a card
  has to authorize (opening the transaction) and the EVSE side has to be
  offering power (the "C switch", power_offered). Either alone leaves the
  connector short of Charging, exactly matching real J1772/IEC 61851 pilot
  signalling.
* A fault does not end the transaction. Confirmed against real VESTEL EVC04
  hardware: the same transactionId keeps appearing in MeterValues before,
  during, and after a fault window, and recovery restores whatever status the
  connector genuinely had right before the fault, rather than guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AvailabilityStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfileStatus,
    ClearCacheStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    RemoteStartStopStatus,
    ResetStatus,
    TriggerMessageStatus,
    UnlockStatus,
)

from .vehicle import Vehicle

log = logging.getLogger(__name__)


class CableLocked(RuntimeError):
    """Raised when something tries to remove a cable the latch is holding."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class Connector:
    """One socket, with the meter register that matters most."""

    connector_id: int
    max_power_kw: float = 11.0
    status: str = ChargePointStatus.available.value
    vehicle: Vehicle | None = None
    transaction_id: int | None = None

    # The card that actually opened the current transaction. Cleared when
    # the transaction closes, same lifecycle as transaction_id -- this is
    # what lets the dashboard show which card is genuinely in use on this
    # connector, rather than only ever showing the next card queued up to
    # swipe next.
    active_id_tag: str | None = None

    # Whatever status this connector was actually showing right before a
    # fault, so clearing the fault can restore that exact status instead of
    # reconstructing a guess from the current flags -- a guess can miss real
    # combinations (SuspendedEVSE with the C switch still on, for instance),
    # where the literal remembered status cannot be wrong.
    pre_fault_status: str | None = None

    # Cumulative lifetime energy register in Wh. Never resets, never goes
    # backwards -- exactly like the real thing.
    meter_wh: float = 120_500.0

    # A 0 W profile installed by the CSMS is what "paused" means here.
    power_limit_w: float | None = None
    active_profile_id: int | None = None

    # Set once the battery is full, so the shutdown only runs once.
    winding_down: bool = False

    # The "C switch": whether the EVSE side is currently offering power at
    # all, independent of whether a transaction has been authorized. Real
    # J1772/IEC 61851 pilot signalling has both halves -- the EV side asking
    # for power (what RFID + StartTransaction represent here) and the EVSE
    # side actually offering it. Both have to be true at once for current to
    # flow; either alone leaves the connector short of Charging.
    power_offered: bool = False

    @property
    def plugged_in(self) -> bool:
        return self.vehicle is not None

    @property
    def suspended_by_evse(self) -> bool:
        return self.power_limit_w is not None and self.power_limit_w <= 0.0

    @property
    def cable_locked(self) -> bool:
        """The connector latch holds the cable captive while power flows.

        Real EVSEs engage the lock for as long as a transaction is open, not
        merely while current is flowing: a held connector can resume at any
        moment, and a cable pulled in between would be pulled live. End the
        transaction before removing it.
        """
        return self.transaction_id is not None

    def limit_kw(self) -> float:
        if self.power_limit_w is None:
            return self.max_power_kw
        return max(0.0, self.power_limit_w / 1000.0)


class SimulatedChargePoint(BaseChargePoint):
    def __init__(
        self,
        identity: str,
        connection: Any,
        *,
        connectors: int = 2,
        max_power_kw: float = 11.0,
        heartbeat_interval: int = 300,
        meter_sample_interval: int = 10,
        tick_seconds: float = 1.0,
        time_scale: float = 1.0,
        full_dwell_seconds: float = 4.0,
        report_vehicle: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(identity, connection)
        self.connectors: dict[int, Connector] = {
            n: Connector(connector_id=n, max_power_kw=max_power_kw)
            for n in range(1, connectors + 1)
        }
        self.heartbeat_interval = heartbeat_interval
        self.meter_sample_interval = meter_sample_interval
        self.tick_seconds = tick_seconds
        # Lets a demo compress an hour of charging into a minute without
        # changing any of the charging logic.
        self.time_scale = time_scale
        # How long the connector reports SuspendedEV before the transaction
        # is closed. Real seconds, not simulated ones.
        self.full_dwell_seconds = full_dwell_seconds
        # Tells the CSMS which car is on a connector. OCPP 1.6 has no message
        # for this, so it is a side channel supplied by the runner.
        self._report_vehicle = report_vehicle
        self.configuration: dict[str, tuple[str, bool]] = {
            "HeartbeatInterval": (str(heartbeat_interval), False),
            "MeterValueSampleInterval": (str(meter_sample_interval), False),
            "NumberOfConnectors": (str(connectors), True),
            "SupportedFeatureProfiles": ("Core,SmartCharging,RemoteTrigger", True),
            "ChargeProfileMaxStackLevel": ("10", True),
        }
        self._tasks: list[asyncio.Task[Any]] = []

    # =====================================================================
    # Startup / shutdown
    # =====================================================================

    async def boot(self) -> None:
        response = await self.call(
            call.BootNotification(
                charge_point_vendor="SimVendor",
                charge_point_model="SimAC22",
                charge_point_serial_number="SIM-0001",
                firmware_version="1.0.0",
            )
        )
        self.heartbeat_interval = int(getattr(response, "interval", 300) or 300)
        log.info(
            "Boot %s, heartbeat every %ss",
            getattr(response, "status", "?"),
            self.heartbeat_interval,
        )

        # Connector 0 refers to the charge point as a whole.
        await self._status(0, ChargePointStatus.available.value)
        for connector in self.connectors.values():
            await self._status(connector.connector_id, connector.status)

        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="sim-heartbeat"),
            asyncio.create_task(self._charging_loop(), name="sim-charging"),
        ]

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    # =====================================================================
    # Background loops
    # =====================================================================

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.call(call.Heartbeat())
            except Exception:
                log.warning("Heartbeat failed", exc_info=True)

    async def _charging_loop(self) -> None:
        """Advance the meter, then emit MeterValues on the sample interval."""
        since_sample = 0.0
        while True:
            await asyncio.sleep(self.tick_seconds)
            simulated = self.tick_seconds * self.time_scale
            for connector in self.connectors.values():
                self._advance_meter(connector, simulated)
                await self._check_full(connector)
            since_sample += self.tick_seconds
            if since_sample >= self.meter_sample_interval:
                since_sample = 0.0
                for connector in self.connectors.values():
                    if connector.transaction_id is not None:
                        try:
                            await self.send_meter_values(connector)
                        except Exception:
                            log.warning("MeterValues failed", exc_info=True)

    async def _check_full(self, connector: Connector) -> None:
        """A full battery: the car stops drawing, then we close the session.

        The order matters and is the order real hardware reports:

          SuspendedEV  the car has stopped accepting energy. The transaction
                       is still open and the charger is still willing to
                       supply -- it is the vehicle that stopped.
          Finishing    only after StopTransaction. The session is over and
                       the connector is waiting for the cable to come out.

        Going straight to Finishing would claim the transaction had ended
        before it had.
        """
        if connector.transaction_id is None or connector.vehicle is None:
            return
        if connector.status == ChargePointStatus.faulted.value:
            # Still genuinely faulted: nothing about the vehicle reaching
            # full changes that, and reporting a fresh status here would be
            # exactly the kind of spontaneous, untriggered re-announcement
            # real hardware never sends. Wait for the fault to actually
            # clear; this check runs again next tick.
            return
        if not connector.vehicle.is_full:
            return
        # A connector held at zero is not finishing, it is held. Without this
        # a car that was already full when the operator pressed Stop would
        # announce SuspendedEV and terminate its own transaction, overriding
        # the hold.
        if connector.suspended_by_evse:
            return

        if connector.winding_down:
            return
        connector.winding_down = True

        log.info(
            "Connector %s: %s is full, the car stopped drawing",
            connector.connector_id, connector.vehicle.name,
        )
        await self._status(
            connector.connector_id, ChargePointStatus.suspended_ev.value
        )

        async def _close_after_dwell() -> None:
            # Real chargers sit in SuspendedEV for a while before terminating
            # -- the car may resume, and ending instantly would be wrong. The
            # dwell also means the state is visible rather than a flicker.
            await asyncio.sleep(self.full_dwell_seconds)
            await self.stop_transaction(connector, reason="Local")

        asyncio.create_task(_close_after_dwell())

    def _advance_meter(self, connector: Connector, seconds: float) -> None:
        """Move the energy register forward, unless something is holding it.

        A paused connector (0 W profile) does not advance, and neither does
        one where the EVSE simply is not offering power yet (the C switch) --
        a transaction being open only means authorization happened, not that
        current is flowing. The register keeps its value either way, which
        is the whole trick behind exact resume.
        """
        if connector.transaction_id is None or connector.vehicle is None:
            return
        if connector.suspended_by_evse or not connector.power_offered:
            return
        power_kw = connector.vehicle.power_at_current_soc(connector.limit_kw())
        if power_kw <= 0.0:
            return
        energy_kwh = power_kw * (seconds / 3600.0)
        connector.meter_wh += energy_kwh * 1000.0
        connector.vehicle.absorb(energy_kwh)

    async def send_meter_values(self, connector: Connector) -> None:
        vehicle = connector.vehicle
        power_kw = (
            0.0
            if connector.suspended_by_evse or not connector.power_offered or vehicle is None
            else vehicle.power_at_current_soc(connector.limit_kw())
        )
        sampled: list[dict[str, Any]] = [
            {
                "value": f"{connector.meter_wh:.0f}",
                "measurand": "Energy.Active.Import.Register",
                "unit": "Wh",
                "context": "Sample.Periodic",
            },
            {
                "value": f"{power_kw * 1000:.0f}",
                "measurand": "Power.Active.Import",
                "unit": "W",
                "context": "Sample.Periodic",
            },
            {
                "value": f"{(power_kw * 1000) / 230.0:.1f}",
                "measurand": "Current.Import",
                "unit": "A",
                "context": "Sample.Periodic",
            },
            {"value": "230.0", "measurand": "Voltage", "unit": "V"},
        ]
        if vehicle is not None:
            sampled.append(
                {
                    "value": f"{vehicle.current_soc:.1f}",
                    "measurand": "SoC",
                    "unit": "Percent",
                    "context": "Sample.Periodic",
                }
            )
        await self.call(
            call.MeterValues(
                connector_id=connector.connector_id,
                transaction_id=connector.transaction_id,
                meter_value=[{"timestamp": utcnow_iso(), "sampled_value": sampled}],
            )
        )

    # =====================================================================
    # Physical actions, driven by the control API
    # =====================================================================

    async def plug_in(self, connector_id: int, vehicle: Vehicle) -> None:
        connector = self.connectors[connector_id]
        connector.vehicle = vehicle
        await self._status(connector_id, ChargePointStatus.preparing.value)
        log.info("Connector %s: %s plugged in", connector_id, vehicle.name)

    async def unplug(self, connector_id: int) -> None:
        connector = self.connectors[connector_id]
        if connector.cable_locked:
            raise CableLocked(
                f"Connector {connector_id} is delivering power and the cable is "
                f"locked. Press Stop first."
            )
        if connector.transaction_id is not None:
            await self.stop_transaction(connector, reason="EVDisconnected")
        connector.vehicle = None
        connector.active_id_tag = None
        connector.power_limit_w = None
        connector.active_profile_id = None
        connector.winding_down = False
        connector.power_offered = False
        await self._status(connector_id, ChargePointStatus.available.value)
        log.info("Connector %s: cable removed", connector_id)

    async def set_power_offered(self, connector_id: int, offered: bool) -> None:
        """The "C switch": whether the EVSE side is offering power at all,
        independent of authorization.

        Confirmed against real hardware: turning this on with no RFID read
        yet leaves the connector stuck at Preparing -- the switch alone
        cannot start anything. Turning it on after a transaction is already
        open (SuspendedEV) is what actually moves to Charging. Turning it off
        again does not end the transaction -- only RemoteStopTransaction or
        the charger's own EVDisconnected path does that -- it just drops back
        to SuspendedEV, since current genuinely stops flowing.
        """
        connector = self.connectors.get(connector_id)
        if connector is None:
            return
        connector.power_offered = offered
        if offered:
            if connector.transaction_id is not None and not connector.suspended_by_evse:
                await self._status(connector_id, ChargePointStatus.charging.value)
            # A 0 W pause profile (the dashboard's Stop / hold) takes
            # precedence over the C switch: reporting Charging here would
            # override a hold the operator explicitly asked for. No
            # transaction yet: nothing to report differently. The connector
            # is already at Preparing from plug_in, and stays there.
            # there -- exactly the "stuck at Preparing" behavior confirmed
            # against real hardware when C is on but RFID has not happened.
        else:
            if connector.transaction_id is not None:
                await self._status(connector_id, ChargePointStatus.suspended_ev.value)
            # No transaction: nothing was flowing anyway, nothing to change.

    async def swipe_card(self, connector_id: int, id_tag: str) -> str:
        """Present a card at the reader.

        This is the only thing that opens a transaction -- confirmed against
        real hardware, not assumed: the reader authorizes and, in the same
        motion, calls StartTransaction. What status follows depends on
        whether the EVSE side is already offering power (the C switch): if
        it is, current flows and this goes straight to Charging; if not, the
        transaction is open but nothing is drawing, which is SuspendedEV.
        Only RemoteStopTransaction (or the charger's own EVDisconnected path)
        ever closes what this opens.

        Returns the idTagInfo status verbatim ("Accepted", "Blocked",
        "Expired", "Invalid", "ConcurrentTx") so the interface can say *why*
        a card was refused.

        One card per session: if this connector already has a card recorded
        for whatever is currently plugged in (including while Finishing,
        since the cable is still in), a different card is refused before
        Authorize even goes out. The session's own id_tag field has room for
        exactly one card, so a second one succeeding here would silently
        overwrite it rather than represent two cards genuinely sharing one
        session, which nothing in the data model actually supports.

        Faulted: refused outright, before Authorize is even sent, matching
        RemoteStartTransaction and RemoteStopTransaction -- one consistent
        rule everywhere a card, a remote start, or a remote stop could act on
        a connector.
        """
        connector = self.connectors.get(connector_id)
        if connector is not None and connector.status == ChargePointStatus.faulted.value:
            log.info(
                "Connector %s: card %s refused -- connector is faulted",
                connector_id, id_tag,
            )
            return "Invalid"
        if (
            connector is not None
            and connector.active_id_tag is not None
            and connector.active_id_tag != id_tag
        ):
            log.info(
                "Connector %s: card %s refused -- %s is already this "
                "session's card",
                connector_id, id_tag, connector.active_id_tag,
            )
            return "ConcurrentTx"
        response = await self.call(call.Authorize(id_tag=id_tag))
        info = getattr(response, "id_tag_info", {}) or {}
        status = str(info.get("status", "Invalid"))
        if status != "Accepted":
            log.info("Card %s refused (%s)", id_tag, status)
            return status
        if connector is not None:
            await self.start_transaction(connector, id_tag)
        return status

    async def _announce_vehicle(self, connector: Connector) -> None:
        """Make sure the CSMS knows which car is on this connector.

        A session is created whenever a transaction opens on a connector that
        has none -- which happens every time charging restarts on a cable that
        never came out, for instance after a full battery or a cleared fault.
        Announcing here rather than only at plug-in means such a session is
        never born anonymous.
        """
        if self._report_vehicle is None or connector.vehicle is None:
            return
        try:
            await self._report_vehicle(connector.connector_id, connector.vehicle.id)
        except Exception:  # noqa: BLE001 - never block charging on this
            log.warning(
                "Could not report the vehicle on connector %s",
                connector.connector_id, exc_info=True,
            )

    async def start_transaction(self, connector: Connector, id_tag: str) -> None:
        await self._announce_vehicle(connector)
        response = await self.call(
            call.StartTransaction(
                connector_id=connector.connector_id,
                id_tag=id_tag,
                meter_start=int(connector.meter_wh),
                timestamp=utcnow_iso(),
            )
        )
        info = getattr(response, "id_tag_info", {}) or {}
        if info.get("status") != "Accepted":
            log.info("StartTransaction refused (%s)", info.get("status"))
            return
        connector.transaction_id = int(response.transaction_id)
        connector.active_id_tag = id_tag
        status = (
            ChargePointStatus.charging.value
            if connector.power_offered
            else ChargePointStatus.suspended_ev.value
        )
        await self._status(connector.connector_id, status)
        log.info(
            "Connector %s: transaction %s started at %.0f Wh",
            connector.connector_id,
            connector.transaction_id,
            connector.meter_wh,
        )

    async def stop_transaction(self, connector: Connector, reason: str) -> None:
        if connector.transaction_id is None:
            return
        transaction_id = connector.transaction_id
        await self.call(
            call.StopTransaction(
                meter_stop=int(connector.meter_wh),
                timestamp=utcnow_iso(),
                transaction_id=transaction_id,
                reason=reason,
            )
        )
        connector.transaction_id = None
        connector.power_limit_w = None
        connector.active_profile_id = None
        connector.winding_down = False
        log.info(
            "Connector %s: transaction %s stopped at %.0f Wh (%s)",
            connector.connector_id,
            transaction_id,
            connector.meter_wh,
            reason,
        )
        if connector.status == ChargePointStatus.faulted.value:
            # Still genuinely faulted: the transaction closing does not mean
            # the fault condition is gone. Report nothing further here --
            # only set_fault(faulted=False), the explicit clear, changes what
            # status this connector reports next.
            return
        if connector.plugged_in:
            # The transaction is closed but the cable is still in. Finishing
            # is where the connector waits, and it stays there until the cable
            # is removed rather than dropping back to a state that would
            # invite an immediate restart.
            await self._status(
                connector.connector_id, ChargePointStatus.finishing.value
            )
        else:
            await self._status(
                connector.connector_id, ChargePointStatus.available.value
            )

    async def set_fault(self, connector_id: int, faulted: bool = True) -> None:
        """Raise or clear a fault.

        Real hardware (confirmed against an actual VESTEL EVC04 this project
        was built against) keeps the transaction running straight through a
        fault rather than stopping it -- the same transactionId keeps
        appearing in MeterValues before, during, and after. Stopping the
        transaction here, as this used to do, taught the CSMS the wrong
        contract: that a fault ends the session and recovery opens a new one.
        It does not. The CSMS's own fault handling was rewritten to match the
        real behavior below; this simulator needs to match it too, or a test
        passing against the simulator would not mean anything against a real
        charger.

        Recovery restores whatever status this connector genuinely had right
        before the fault -- Charging, SuspendedEV, SuspendedEVSE, Finishing,
        Preparing, whatever it actually was -- rather than reconstructing a
        guess from the current flags. A guess based on flags alone missed
        real combinations (a connector held at zero with the C switch still
        on reports SuspendedEVSE, for example, which is easy to derive
        wrong); remembering the literal status beforehand and restoring it
        exactly cannot make that mistake.
        """
        connector = self.connectors.get(connector_id)
        if faulted:
            # Remember what this connector was actually showing right before
            # the fault, so recovery can restore that exact status instead of
            # guessing -- a connector that faulted while Finishing should
            # come back to Finishing, not be silently reset to Preparing as
            # if the cable had just been plugged in.
            if connector is not None:
                connector.pre_fault_status = connector.status
            # Report the fault. The transaction is left running -- no
            # StopTransaction, no change to connector.transaction_id -- so
            # MeterValues can keep flowing under it exactly as a real fault
            # window does.
            await self._status(
                connector_id,
                ChargePointStatus.faulted.value,
                error_code=ChargePointErrorCode.ground_failure.value,
            )
            return

        # Recovering: restore whatever this connector was actually showing
        # right before the fault hit, whenever that is known. This is
        # deliberately not re-derived from the current flags (transaction_id,
        # power_offered, a pause profile) -- a fault can land on any real
        # combination of those (SuspendedEVSE while still offering power,
        # SuspendedEV with it off, mid-transaction Charging, and so on), and
        # trying to reconstruct which one it was from the flags alone is
        # exactly what missed SuspendedEVSE the first time this was written.
        # pre_fault_status is the literal truth, not a guess, so it always
        # wins when it exists.
        if connector is not None and connector.pre_fault_status is not None:
            recovered = connector.pre_fault_status
        elif connector is not None and connector.plugged_in:
            recovered = ChargePointStatus.preparing.value
        else:
            recovered = ChargePointStatus.available.value
        if connector is not None:
            connector.pre_fault_status = None
        await self._status(connector_id, recovered)

    async def _status(
        self,
        connector_id: int,
        status: str,
        error_code: str = ChargePointErrorCode.no_error.value,
    ) -> None:
        if connector_id in self.connectors:
            self.connectors[connector_id].status = status
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=error_code,
                status=status,
                timestamp=utcnow_iso(),
            )
        )

    # =====================================================================
    # Inbound: commands from the CSMS
    #
    # Anything that would itself send a CALL is deferred to a task. The CSMS
    # is blocked waiting for our CALLRESULT, and the library allows only one
    # outstanding CALL per side -- replying first and acting second is what
    # keeps both sides from deadlocking on each other.
    # =====================================================================

    @on(Action.remote_start_transaction)
    async def on_remote_start(
        self, id_tag: str, **kwargs: Any
    ) -> call_result.RemoteStartTransaction:
        connector_id = kwargs.get("connector_id") or 1
        connector = self.connectors.get(connector_id)
        if connector is None or not connector.plugged_in:
            return call_result.RemoteStartTransaction(
                status=RemoteStartStopStatus.rejected
            )
        if connector.status == ChargePointStatus.faulted.value:
            return call_result.RemoteStartTransaction(
                status=RemoteStartStopStatus.rejected
            )
        if connector.transaction_id is not None:
            return call_result.RemoteStartTransaction(
                status=RemoteStartStopStatus.rejected
            )
        asyncio.create_task(self.start_transaction(connector, id_tag))
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop(
        self, transaction_id: int
    ) -> call_result.RemoteStopTransaction:
        for connector in self.connectors.values():
            if connector.transaction_id == transaction_id:
                if connector.status == ChargePointStatus.faulted.value:
                    return call_result.RemoteStopTransaction(
                        status=RemoteStartStopStatus.rejected
                    )
                asyncio.create_task(self.stop_transaction(connector, reason="Remote"))
                return call_result.RemoteStopTransaction(
                    status=RemoteStartStopStatus.accepted
                )
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)

    @on(Action.set_charging_profile)
    async def on_set_charging_profile(
        self, connector_id: int, cs_charging_profiles: dict
    ) -> call_result.SetChargingProfile:
        """A 0 W limit is how the CSMS pauses us.

        The transaction stays open and the meter register keeps its value; we
        simply stop advancing it.
        """
        connector = self.connectors.get(connector_id)
        if connector is None:
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)

        schedule = cs_charging_profiles.get("charging_schedule") or {}
        periods = schedule.get("charging_schedule_period") or [{}]
        limit = float(periods[0].get("limit", 0.0))
        unit = schedule.get("charging_rate_unit", "W")
        limit_w = limit if unit == "W" else limit * 230.0  # amps -> watts

        connector.power_limit_w = limit_w
        connector.active_profile_id = cs_charging_profiles.get("charging_profile_id")

        if connector.transaction_id is not None:
            if limit_w <= 0:
                next_status = ChargePointStatus.suspended_evse.value
            elif connector.power_offered:
                next_status = ChargePointStatus.charging.value
            else:
                # The EVSE-side hold is over, but the C switch was never on
                # to begin with -- that is a separate thing withholding
                # power, not this profile, so this must not claim Charging.
                next_status = ChargePointStatus.suspended_ev.value
            asyncio.create_task(self._status(connector_id, next_status))
        log.info(
            "Connector %s: profile %s applied, limit %.0f W",
            connector_id,
            connector.active_profile_id,
            limit_w,
        )
        return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)

    @on(Action.clear_charging_profile)
    async def on_clear_charging_profile(
        self, **kwargs: Any
    ) -> call_result.ClearChargingProfile:
        profile_id = kwargs.get("id")
        connector_id = kwargs.get("connector_id")
        targets = (
            [self.connectors[connector_id]]
            if connector_id in self.connectors
            else list(self.connectors.values())
        )
        cleared = False
        for connector in targets:
            if profile_id is not None and connector.active_profile_id != profile_id:
                continue
            if connector.power_limit_w is not None:
                cleared = True
            connector.power_limit_w = None
            connector.active_profile_id = None
            if connector.transaction_id is not None:
                next_status = (
                    ChargePointStatus.charging.value
                    if connector.power_offered
                    else ChargePointStatus.suspended_ev.value
                )
                asyncio.create_task(
                    self._status(connector.connector_id, next_status)
                )
        log.info("ClearChargingProfile id=%s -> cleared=%s", profile_id, cleared)
        return call_result.ClearChargingProfile(
            status=ClearChargingProfileStatus.accepted
            if cleared
            else ClearChargingProfileStatus.unknown
        )

    @on(Action.get_configuration)
    async def on_get_configuration(self, **kwargs: Any) -> call_result.GetConfiguration:
        keys = kwargs.get("key") or list(self.configuration)
        known = [
            {
                "key": k,
                "value": self.configuration[k][0],
                "readonly": self.configuration[k][1],
            }
            for k in keys
            if k in self.configuration
        ]
        unknown = [k for k in keys if k not in self.configuration]
        return call_result.GetConfiguration(
            configuration_key=known, unknown_key=unknown or None
        )

    @on(Action.change_configuration)
    async def on_change_configuration(
        self, key: str, value: str
    ) -> call_result.ChangeConfiguration:
        if key in self.configuration and self.configuration[key][1]:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        self.configuration[key] = (value, False)
        try:
            if key == "MeterValueSampleInterval":
                self.meter_sample_interval = max(1, int(value))
            elif key == "HeartbeatInterval":
                self.heartbeat_interval = max(1, int(value))
        except ValueError:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    @on(Action.reset)
    async def on_reset(self, type: str) -> call_result.Reset:
        log.info("%s reset requested", type)
        return call_result.Reset(status=ResetStatus.accepted)

    @on(Action.unlock_connector)
    async def on_unlock(self, connector_id: int) -> call_result.UnlockConnector:
        return call_result.UnlockConnector(status=UnlockStatus.unlocked)

    @on(Action.trigger_message)
    async def on_trigger_message(
        self, requested_message: str, **kwargs: Any
    ) -> call_result.TriggerMessage:
        connector_id = kwargs.get("connector_id") or 1

        async def _respond() -> None:
            await asyncio.sleep(0.05)
            connector = self.connectors.get(connector_id)
            if requested_message == "StatusNotification" and connector:
                await self._status(connector_id, connector.status)
            elif requested_message == "Heartbeat":
                await self.call(call.Heartbeat())
            elif requested_message == "MeterValues" and connector:
                if connector.transaction_id is not None:
                    await self.send_meter_values(connector)

        asyncio.create_task(_respond())
        return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)

    @on(Action.change_availability)
    async def on_change_availability(
        self, connector_id: int, type: str
    ) -> call_result.ChangeAvailability:
        return call_result.ChangeAvailability(status=AvailabilityStatus.accepted)

    @on(Action.get_diagnostics)
    async def on_get_diagnostics(self, location, **_):
        """Pretend to upload logs, reporting progress as a real charger would.

        The filename is returned at once; the Uploading and Uploaded statuses
        follow on a timer, which is what a dashboard watching diagnostics
        progress needs to see.
        """
        name = f"{self.id}-diagnostics-{utcnow_iso()}.log"
        log.info("GetDiagnostics -> %s (to %s)", name, location)

        async def progress():
            for stage in ("Uploading", "Uploaded"):
                await asyncio.sleep(1.5)
                try:
                    await self.call(
                        call.DiagnosticsStatusNotification(status=stage)
                    )
                except Exception:  # noqa: BLE001 - progress is best effort
                    return

        asyncio.create_task(progress())
        return call_result.GetDiagnostics(file_name=name)

    @on(Action.clear_cache)
    async def on_clear_cache(self) -> call_result.ClearCache:
        return call_result.ClearCache(status=ClearCacheStatus.accepted)

    @on(Action.data_transfer)
    async def on_data_transfer(
        self, vendor_id: str, **kwargs: Any
    ) -> call_result.DataTransfer:
        return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)