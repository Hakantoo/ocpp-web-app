"""Runs a pool of simulated charge points and exposes a small control API.

    python -m simulator.main

The cars it can plug in come from the CSMS, not from a list in this file.
That is the point: one source of truth means a car created in the dashboard is
immediately pluggable, and the same car cannot appear to be two different cars
on two connectors.

Any number of chargers can run at once, each its own independent WebSocket
connection to the CSMS with its own identity and connector count -- exactly
how two real chargers would look to the CSMS, which already discovers each
one's connector count itself and has never assumed there is only one charger.
Add one at runtime rather than editing this file and restarting:

    curl -X POST localhost:9100/chargers -d '{"identity":"CP002","connectors":2}' \
        -H 'Content-Type: application/json'

Drive the hardware with plain HTTP, now identity-scoped since more than one
charger can be running:

    curl -X POST localhost:9100/plug   -d '{"identity":"CP001","connector_id":1,"vehicle_id":1}' -H 'Content-Type: application/json'
    curl -X POST localhost:9100/swipe  -d '{"identity":"CP001","connector_id":1,"id_tag":"RFID-0001"}' -H 'Content-Type: application/json'
    curl -X POST localhost:9100/unplug -d '{"identity":"CP001","connector_id":1}' -H 'Content-Type: application/json'
    curl -X POST localhost:9100/fault  -d '{"identity":"CP001","connector_id":1,"faulted":true}' -H 'Content-Type: application/json'
    curl        localhost:9100/chargers
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .charge_point import CableLocked, Connector, SimulatedChargePoint
from .vehicle import Vehicle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s simulator: %(message)s",
    datefmt="%H:%M:%S",
)
# The ocpp library logs every frame at INFO; too noisy for a demo.
logging.getLogger("ocpp").setLevel(logging.WARNING)
log = logging.getLogger("simulator")

CSMS_WS = os.getenv("SIM_CSMS_URL", "ws://localhost:9000/ocpp")
CSMS_HTTP = os.getenv("SIM_CSMS_HTTP", "http://localhost:9000")
DEFAULT_IDENTITY = os.getenv("SIM_IDENTITY", "CP001")
DEFAULT_CONNECTORS = int(os.getenv("SIM_CONNECTORS", "2"))
CONTROL_PORT = int(os.getenv("SIM_CONTROL_PORT", "9100"))
TIME_SCALE = float(os.getenv("SIM_TIME_SCALE", "60"))
SAMPLE_INTERVAL = int(os.getenv("SIM_SAMPLE_INTERVAL", "5"))


class AddChargerRequest(BaseModel):
    identity: str = Field(min_length=1)
    connectors: int = Field(2, ge=1, le=8)
    label: str | None = None
    max_power_kw: float | None = None
    heartbeat_interval: int | None = None
    registration_status: str | None = None
    supports_charging_profiles: bool | None = None
    require_card_before_start: bool | None = None
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    time_scale: float | None = None
    meter_sample_interval: int | None = None
    full_dwell_seconds: float | None = None


class SettingsRequest(BaseModel):
    identity: str
    time_scale: float | None = Field(None, gt=0)
    meter_sample_interval: int | None = Field(None, ge=1)
    max_power_kw: float | None = Field(None, gt=0)
    full_dwell_seconds: float | None = Field(None, ge=0)


class ConnectionRequest(BaseModel):
    identity: str


class PlugRequest(BaseModel):
    identity: str
    connector_id: int = Field(1, ge=1)
    vehicle_id: int


class SwipeRequest(BaseModel):
    identity: str
    connector_id: int = Field(1, ge=1)
    id_tag: str


class ConnectorRequest(BaseModel):
    identity: str
    connector_id: int = Field(1, ge=1)


class FaultRequest(BaseModel):
    identity: str
    connector_id: int = Field(1, ge=1)
    faulted: bool = True


class PowerRequest(BaseModel):
    identity: str
    connector_id: int = Field(1, ge=1)
    offered: bool = True


# Every currently-running charger, keyed by identity. A charger appears here
# once its WebSocket connection is up, and disappears while it is reconnecting
# after a drop -- the same charger's task keeps retrying with backoff, it just
# is not in this dict in the meantime, matching how a real one would look
# absent from the CSMS's own registry while offline.
running: dict[str, SimulatedChargePoint] = {}
# One retry-with-backoff task per identity, alive for as long as that charger
# is meant to exist (added once, kept running -- including through drops --
# until the process itself stops, or it is explicitly removed).
tasks: dict[str, asyncio.Task] = {}
# How many connectors each charger was created with, kept so a reconnect (or
# a manual online toggle) recreates it with the same shape rather than
# guessing.
connector_counts: dict[str, int] = {}
# Set while an operator has deliberately asked a charger to go offline. The
# retry loop checks this before reconnecting rather than only on the way in,
# so flipping this while already connected needs the live socket actively
# closed too -- see disconnect_charger.
held_offline: dict[str, bool] = {}
# The live websocket connection object per identity, tracked separately from
# `running` (which holds the higher-level SimulatedChargePoint) so it can be
# closed directly on demand -- .close() here is a real disconnect, the same
# as a cable being pulled, not a polite shutdown the charger negotiates.
sockets: dict[str, Any] = {}
# A real charger never forgets what is physically plugged in just because its
# network connection dropped -- only its OCPP session resets, not its own
# hardware state. Every SimulatedChargePoint is a fresh object on each
# reconnect though, with fresh, blank Connector objects, so without this the
# simulator "forgot" which car was plugged in, what the meter read, and
# whether a transaction was running, the instant a connection dropped and
# came back. Keyed by identity, then by connector_id.
persisted_connectors: dict[str, dict[int, Connector]] = {}


def _cp(identity: str) -> SimulatedChargePoint:
    cp = running.get(identity)
    if cp is None:
        raise HTTPException(
            503, f"{identity} is not connected to the CSMS yet (or does not exist)"
        )
    return cp


async def fetch_vehicles() -> list[Vehicle]:
    """Read the car list from the CSMS. One list, one truth."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{CSMS_HTTP}/api/vehicles")
        response.raise_for_status()
        return [Vehicle.from_api(row) for row in response.json()]


def _report_vehicle_for(identity: str):
    """Bind a report_vehicle callback to one charger's identity.

    Every charger needs its own, since the CSMS endpoint this calls is scoped
    by charger identity in the URL -- reporting connector 1's car on CP002
    must not be sent to CP001's path.
    """

    async def report_vehicle(connector_id: int, vehicle_id: int) -> None:
        """Tell the CSMS which car is on this connector.

        OCPP 1.6 has no message for this, so it goes over a side channel. Real
        hardware cannot do it, and with a physical charger a session simply
        has no vehicle attached -- worth remembering before reading much into
        this.

        Called at plug-in, when a fault clears with the cable still
        connected, and whenever a transaction opens. The last two matter
        because each of them can make the CSMS create a fresh session on a
        cable that never came out.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{CSMS_HTTP}/api/charge-points/{identity}/vehicle",
                json={"connector_id": connector_id, "vehicle_id": vehicle_id},
            )
            if response.status_code >= 400:
                detail = response.json().get("detail", response.text)
                raise HTTPException(response.status_code, detail)

    return report_vehicle


async def _add_default_charger() -> None:
    """Provision the default charger the same real way any other simulated
    charger is created, marked is_simulated, before starting its connection.

    Never connects a WebSocket for an identity that provisioning has not
    actually succeeded for. A previous version fell through to connecting
    anyway when provisioning failed, which opened a real window: the
    WebSocket would boot before the CSMS had any record marked
    is_simulated, and the CSMS's own auto-registration for a genuinely
    unknown charger would win the race, creating an ambiguous
    "Unprovisioned CP001" indistinguishable from real hardware. Retrying
    with backoff until provisioning genuinely succeeds closes that race
    instead of racing it.
    """
    backoff = 1.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{CSMS_HTTP}/api/charge-points",
                    json={
                        "identity": DEFAULT_IDENTITY,
                        "connector_count": DEFAULT_CONNECTORS,
                        "is_simulated": True,
                    },
                )
            # 409 means it already exists from a previous run -- already
            # marked correctly then, provisioning has effectively succeeded.
            if response.status_code in (200, 409):
                break
            log.warning(
                "Could not provision the default charger %s (%s); "
                "retrying in %.0fs",
                DEFAULT_IDENTITY, response.status_code, backoff,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "Could not reach the CSMS to provision %s; retrying in %.0fs",
                DEFAULT_IDENTITY, backoff,
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
    await add_charger(DEFAULT_IDENTITY, DEFAULT_CONNECTORS)


async def _reconnect_known_chargers() -> None:
    """The CSMS's own database is the source of truth for which *simulated*
    chargers exist, not this process's memory -- which is exactly why
    restarting the simulator used to lose every one of them except one
    hardcoded default. On boot, ask the CSMS what it already knows about,
    keep only the rows marked is_simulated, and reconnect to each of those
    with its real connector count.

    Real hardware the CSMS also knows about is deliberately left alone here:
    it is never ours to reconnect to, edit, or mark online on its behalf, and
    the is_simulated flag (set only by this file's own provisioning call) is
    exactly what tells the two apart.

    If the CSMS is not reachable yet, this waits and retries with backoff
    rather than giving up after one attempt -- a CSMS that is merely still
    starting up is not a reason to fabricate a default charger, since doing
    that let a genuinely unprovisioned identity connect and get silently
    auto-registered by the CSMS's own catch-all for unknown hardware. Only
    once the CSMS is confirmed reachable and genuinely has no simulated
    charger at all -- a real first-ever run, not a timing race -- does this
    fall back to provisioning the default charger.
    """
    backoff = 1.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{CSMS_HTTP}/api/charge-points")
                response.raise_for_status()
                chargers = response.json()
            break
        except Exception:  # noqa: BLE001
            log.warning(
                "Could not reach the CSMS to list existing chargers; "
                "retrying in %.0fs",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    simulated = [cp for cp in chargers if cp.get("is_simulated")]
    if not simulated:
        await _add_default_charger()
        return

    for cp in simulated:
        identity = cp["identity"]
        # connector 0 represents the unit itself, not a physical socket --
        # every real count is one less than the rows returned.
        physical = [c for c in cp.get("connectors", []) if c.get("connector_id", 0) > 0]
        await add_charger(identity, len(physical) or DEFAULT_CONNECTORS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _reconnect_known_chargers()
    try:
        yield
    finally:
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Charge point simulator", version="0.3.0", lifespan=lifespan)


def _connector_state(c) -> dict:
    return {
        "connector_id": c.connector_id,
        "status": c.status,
        "meter_wh": round(c.meter_wh, 1),
        "transaction_id": c.transaction_id,
        "power_limit_w": c.power_limit_w,
        "paused": c.suspended_by_evse,
        "cable_locked": c.cable_locked,
        "power_offered": c.power_offered,
        "active_id_tag": c.active_id_tag,
        "vehicle": None
        if c.vehicle is None
        else {
            "id": c.vehicle.id,
            "name": c.vehicle.name,
            "soc": round(c.vehicle.current_soc, 1),
            "capacity_kwh": c.vehicle.battery_capacity_kwh,
        },
    }


@app.get("/chargers")
async def list_chargers() -> dict:
    """Every charger that has been added, connected or not -- a charger still
    reconnecting after a drop is worth showing rather than silently
    disappearing, the same way it would still show up (offline) on the
    dashboard rather than vanishing."""
    return {
        "chargers": [
            {
                "identity": identity,
                "connected": identity in running,
                "held_offline": held_offline.get(identity, False),
                "time_scale": running[identity].time_scale if identity in running else None,
                "meter_sample_interval": running[identity].meter_sample_interval
                if identity in running
                else None,
                "full_dwell_seconds": running[identity].full_dwell_seconds
                if identity in running
                else None,
                "max_power_kw": next(iter(running[identity].connectors.values())).max_power_kw
                if identity in running and running[identity].connectors
                else None,
                "connectors": [
                    _connector_state(c) for c in running[identity].connectors.values()
                ]
                if identity in running
                else [],
            }
            for identity in tasks
        ]
    }


@app.get("/state")
async def get_state(identity: str = DEFAULT_IDENTITY) -> dict:
    """Kept for anything still calling the old single-charger shape; prefer
    /chargers, which lists all of them."""
    cp = _cp(identity)
    return {
        "identity": cp.id,
        "time_scale": cp.time_scale,
        "connectors": [_connector_state(c) for c in cp.connectors.values()],
    }


@app.post("/chargers")
async def add_charger_endpoint(body: AddChargerRequest) -> dict:
    """Provisions the charger in the CSMS's own database first -- the exact
    same POST /api/charge-points the dashboard's own "Create charger" form
    calls -- and only then starts this process's WebSocket connection to it.

    Without the first half, a simulated charger only ever existed in this
    process's memory: the CSMS never knew about it until BootNotification
    created a row on the fly, and restarting the simulator lost that memory
    while the CSMS's row (and its "last seen" history) stayed behind,
    stranded and looking like a dead charger with no way to remove it short
    of touching the database by hand. Provisioning through the real endpoint
    first means a simulated charger is created exactly the way a real one
    would be pre-registered, and deleting it (see remove_charger below)
    genuinely removes it rather than just disconnecting a socket.
    """
    if body.identity in tasks:
        raise HTTPException(409, f"{body.identity} already exists")
    payload = {
        "identity": body.identity,
        "connector_count": body.connectors,
        "is_simulated": True,
    }
    # Only forward fields actually supplied -- an omitted key lets the CSMS's
    # own defaults apply, same reasoning as the dashboard's own create form:
    # a blank field means "same as if the charger had not told us yet", not
    # a made-up value invented here.
    optional = {
        "label": body.label,
        "max_power_kw": body.max_power_kw,
        "heartbeat_interval": body.heartbeat_interval,
        "registration_status": body.registration_status,
        "supports_charging_profiles": body.supports_charging_profiles,
        "require_card_before_start": body.require_card_before_start,
        "vendor": body.vendor,
        "model": body.model,
        "serial_number": body.serial_number,
        "firmware_version": body.firmware_version,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(f"{CSMS_HTTP}/api/charge-points", json=payload)
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text)
            raise HTTPException(response.status_code, detail)
    await add_charger(
        body.identity,
        body.connectors,
        time_scale=body.time_scale,
        meter_sample_interval=body.meter_sample_interval,
        full_dwell_seconds=body.full_dwell_seconds,
    )
    return {"ok": True, "identity": body.identity, "connectors": body.connectors}


@app.delete("/chargers/{identity}")
async def remove_charger(identity: str) -> dict:
    """Stops this process's connection to the charger, then deletes its row
    from the CSMS's own database via the same DELETE endpoint the dashboard's
    Chargers page uses -- one removal path, not a local one that leaves the
    real record behind."""
    task = tasks.pop(identity, None)
    if task is None:
        raise HTTPException(404, f"No such charger: {identity}")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    held_offline.pop(identity, None)
    connector_counts.pop(identity, None)
    sockets.pop(identity, None)
    running.pop(identity, None)
    persisted_connectors.pop(identity, None)

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.delete(f"{CSMS_HTTP}/api/charge-points/{identity}")
        # 404 here just means the CSMS never actually saw this charger --
        # nothing wrong, since the point is the row not existing either way.
        # A genuine failure (409: a session is still open) should surface,
        # since silently leaving the row behind is exactly the drift this
        # exists to prevent.
        if response.status_code >= 400 and response.status_code != 404:
            detail = response.json().get("detail", response.text)
            raise HTTPException(response.status_code, detail)

    return {"ok": True, "identity": identity}


@app.post("/chargers/{identity}/offline")
async def take_offline(identity: str) -> dict:
    """A genuine disconnect, the same as a cable being pulled or the network
    dropping -- not a polite goodbye the charger negotiates with the CSMS.
    The retry loop is told to stay down until told otherwise, so it does not
    immediately reconnect the instant this closes the socket."""
    if identity not in tasks:
        raise HTTPException(404, f"No such charger: {identity}")
    held_offline[identity] = True
    ws = sockets.get(identity)
    if ws is not None:
        await ws.close()
    return {"ok": True, "identity": identity, "connected": False}


@app.post("/chargers/{identity}/online")
async def bring_online(identity: str) -> dict:
    """Lets the retry loop resume connecting. Whether it is actually
    connected a moment later depends on the CSMS being reachable, same as any
    real reconnect -- this only lifts the deliberate hold, it does not force
    an instant connection."""
    if identity not in tasks:
        raise HTTPException(404, f"No such charger: {identity}")
    held_offline[identity] = False
    return {"ok": True, "identity": identity}


@app.patch("/chargers/{identity}/settings")
async def update_settings(identity: str, body: SettingsRequest) -> dict:
    """Every field here is a plain mutable attribute the running loops read
    fresh each iteration, so a change takes effect on the charger's very next
    tick -- no reconnect needed, same as adjusting a real bench supply."""
    cp = _cp(identity)
    changed: dict[str, float | int] = {}
    if body.time_scale is not None:
        cp.time_scale = body.time_scale
        changed["time_scale"] = body.time_scale
    if body.meter_sample_interval is not None:
        cp.meter_sample_interval = body.meter_sample_interval
        changed["meter_sample_interval"] = body.meter_sample_interval
    if body.full_dwell_seconds is not None:
        cp.full_dwell_seconds = body.full_dwell_seconds
        changed["full_dwell_seconds"] = body.full_dwell_seconds
    if body.max_power_kw is not None:
        for connector in cp.connectors.values():
            connector.max_power_kw = body.max_power_kw
        changed["max_power_kw"] = body.max_power_kw
    return {"ok": True, "identity": identity, "changed": changed}


@app.post("/plug")
async def plug(body: PlugRequest) -> dict:
    cp = _cp(body.identity)
    vehicles = {v.id: v for v in await fetch_vehicles()}
    vehicle = vehicles.get(body.vehicle_id)
    if vehicle is None:
        raise HTTPException(404, f"No vehicle {body.vehicle_id}")

    # Ask the CSMS first. It owns the rule that a car is only ever in one
    # socket, so letting it refuse before we move any hardware keeps the two
    # sides from disagreeing.
    await _report_vehicle_for(body.identity)(body.connector_id, body.vehicle_id)
    await cp.plug_in(body.connector_id, vehicle)
    return {"ok": True, "connector_id": body.connector_id, "vehicle": vehicle.name}


@app.post("/unplug")
async def unplug(body: ConnectorRequest) -> dict:
    try:
        await _cp(body.identity).unplug(body.connector_id)
    except CableLocked as exc:
        # 409: the request is well formed but conflicts with physical state.
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "connector_id": body.connector_id}


@app.post("/swipe")
async def swipe(body: SwipeRequest) -> dict:
    status = await _cp(body.identity).swipe_card(body.connector_id, body.id_tag)
    return {"ok": status == "Accepted", "status": status, "id_tag": body.id_tag}


@app.post("/fault")
async def fault(body: FaultRequest) -> dict:
    cp = _cp(body.identity)
    await cp.set_fault(body.connector_id, body.faulted)

    # If a transaction was already running when the fault hit, set_fault
    # reports Charging directly on recovery, so the CSMS resumes the same
    # session it already has open -- nothing to re-report there. This only
    # matters for the other case: a fault clearing with the cable still
    # connected but no transaction running, where the CSMS opens a fresh
    # session and nothing else would tell it which car is sitting there, since
    # the vehicle was only ever reported at the original plug-in.
    if not body.faulted:
        connector = cp.connectors.get(body.connector_id)
        if connector is not None and connector.vehicle is not None:
            await _report_vehicle_for(body.identity)(
                body.connector_id, connector.vehicle.id
            )

    return {"ok": True, "faulted": body.faulted}


@app.post("/power")
async def power(body: PowerRequest) -> dict:
    """The "C switch": whether the EVSE side is offering power at all.

    Confirmed against real hardware: turning this on with no RFID read yet
    leaves the connector stuck at Preparing. Turning it on after a card has
    already been read (SuspendedEV) is what actually moves to Charging.
    Turning it off drops back to SuspendedEV rather than ending anything --
    only RemoteStopTransaction or an unplug ever closes the transaction.
    """
    await _cp(body.identity).set_power_offered(body.connector_id, body.offered)
    return {"ok": True, "offered": body.offered}


async def add_charger(
    identity: str,
    connectors: int,
    *,
    time_scale: float | None = None,
    meter_sample_interval: int | None = None,
    full_dwell_seconds: float | None = None,
) -> None:
    """Start a charger's connection task. Called once at startup for the
    default charger, and once per /chargers POST after that -- both go
    through the same path, so an added charger behaves identically to the
    one that was always there."""
    if identity in tasks:
        return
    tasks[identity] = asyncio.create_task(
        run_charge_point(
            identity,
            connectors,
            time_scale=time_scale,
            meter_sample_interval=meter_sample_interval,
            full_dwell_seconds=full_dwell_seconds,
        ),
        name=f"sim-connection-{identity}",
    )


async def run_charge_point(
    identity: str,
    connectors: int,
    *,
    time_scale: float | None = None,
    meter_sample_interval: int | None = None,
    full_dwell_seconds: float | None = None,
) -> None:
    """Connect, run, and reconnect with backoff if the CSMS goes away.

    One of these runs per charger, entirely independent of any other -- one
    charger's drop and reconnect never touches another's connection, the same
    as two real chargers plugged into two different network ports.

    Checks held_offline before every connection attempt, not only once at the
    top: an operator can ask a running charger to go offline mid-loop, and
    the next reconnect attempt (after the current socket is closed by
    disconnect_charger) must not immediately undo that.

    Also restores whatever this charger's connectors were physically holding
    before the previous drop (persisted_connectors) -- a fresh
    SimulatedChargePoint is a fresh object with blank Connector state, and
    without this a genuine WebSocket reconnect would "forget" a plugged-in
    car, an open transaction, or the meter reading, even though none of that
    would ever be forgotten by a real charger just because its network
    connection blinked.
    """
    connector_counts[identity] = connectors
    backoff = 1.0
    while True:
        if held_offline.get(identity):
            await asyncio.sleep(0.5)
            continue
        url = f"{CSMS_WS}/{identity}"
        cp: SimulatedChargePoint | None = None
        try:
            log.info("Connecting %s to %s", identity, url)
            async with websockets.connect(url, subprotocols=["ocpp1.6"]) as ws:
                if held_offline.get(identity):
                    # Set true in the gap between this loop's own
                    # held_offline check and the connection actually
                    # finishing -- take_offline's own ws.close() may have
                    # closed a *previous* socket, or found none registered
                    # yet, and missed this one entirely. Close it ourselves
                    # before boot() ever runs, or this connection would boot
                    # and only die once the loop happens to come back around.
                    log.info(
                        "%s asked to go offline mid-connect; closing before boot",
                        identity,
                    )
                    continue
                sockets[identity] = ws
                cp = SimulatedChargePoint(
                    identity,
                    ws,
                    connectors=connectors,
                    meter_sample_interval=meter_sample_interval or SAMPLE_INTERVAL,
                    time_scale=time_scale or TIME_SCALE,
                    **(
                        {"full_dwell_seconds": full_dwell_seconds}
                        if full_dwell_seconds is not None
                        else {}
                    ),
                    report_vehicle=_report_vehicle_for(identity),
                )
                # A reconnect builds a brand new object with brand new, blank
                # Connector state -- restore whatever was true before the
                # drop, the same way a real charger's own memory of what is
                # plugged in survives its network dropping, only the OCPP
                # session resets.
                prior = persisted_connectors.get(identity)
                if prior:
                    for connector_id, saved in prior.items():
                        if connector_id in cp.connectors:
                            cp.connectors[connector_id] = saved
                running[identity] = cp
                backoff = 1.0
                await asyncio.gather(cp.start(), cp.boot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s connection lost (%s); retrying in %.0fs", identity, exc, backoff
            )
        finally:
            if cp is not None:
                # Save what was physically true before this object goes away
                # -- the next reconnect needs it back, the same way a real
                # charger never forgets what is plugged in just because its
                # network connection dropped.
                persisted_connectors[identity] = dict(cp.connectors)
                await cp.shutdown()
            running.pop(identity, None)
            sockets.pop(identity, None)
        if held_offline.get(identity):
            continue
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=CONTROL_PORT, log_level="warning")