"""REST API for the dashboard.

Thin by design: routers validate input, call a domain service or a repository,
and return plain dicts. No business rules live here, and every guard that
protects data lives below this layer so a hand-rolled request cannot skip it.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from typing import Any

import aiosqlite
from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..db.database import Database, to_db, utcnow
from ..db.enums import StopReason
from ..domain.ports import CommandError
from ..domain.sessions import SessionError, SessionService
from ..repository import charge_points as cp_repo
from ..repository import faults as faults_repo
from ..repository import messages as messages_repo
from ..repository import metering as metering_repo
from ..repository import sessions as sessions_repo
from ..repository import tags as tags_repo
from .diagnostics import DIAGNOSTICS_DIR
from ..repository import uptime as uptime_repo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _own_lan_ip() -> str:
    """The address of the interface that reaches the outside world, which is
    the one a charger on the same network can reach. Falls back to localhost
    only if there is no route at all."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _db(request: Request) -> Database:
    return request.app.state.db


def _sessions(request: Request) -> SessionService:
    return request.app.state.sessions


def _handle(exc: Exception) -> HTTPException:
    """Domain failures are client errors, not 500s."""
    if isinstance(exc, SessionError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CommandError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _conflict(exc: Exception) -> HTTPException:
    """Turn a constraint violation into something an operator can act on."""
    message = str(exc)
    if "ux_sessions_one_open_per_vehicle" in message:
        return HTTPException(409, "That car is already plugged in somewhere else")
    if "UNIQUE" in message:
        return HTTPException(409, "That already exists")
    if "FOREIGN KEY" in message:
        return HTTPException(409, "That refers to something which does not exist")
    if "CHECK" in message:
        return HTTPException(422, "A value is outside its allowed range")
    return HTTPException(400, message)


INTEGRITY_ERRORS = (sqlite3.IntegrityError, aiosqlite.IntegrityError)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class StartRequest(BaseModel):
    connector_id: int = Field(ge=1)
    id_tag: str | None = None


class ConfigRequest(BaseModel):
    key: str
    value: str


class AttachVehicleRequest(BaseModel):
    """Sent by the charger when a car is connected.

    A lab channel: OCPP 1.6 has no way to report vehicle identity, so real
    hardware will never call this and its sessions simply have no vehicle.
    """

    connector_id: int = Field(ge=1)
    vehicle_id: int


class TagBody(BaseModel):
    id_tag: str = Field(min_length=1, max_length=20)
    status: str = "Accepted"
    expiry_date: str | None = None


class TagPatch(BaseModel):
    status: str | None = None
    expiry_date: str | None = None


class VehicleBody(BaseModel):
    name: str = Field(min_length=1)
    battery_capacity_kwh: float = Field(gt=0)
    max_charge_kw: float = Field(default=11.0, gt=0)
    current_soc: float = Field(default=20.0, ge=0, le=100)


class VehiclePatch(BaseModel):
    name: str | None = None
    battery_capacity_kwh: float | None = Field(default=None, gt=0)
    max_charge_kw: float | None = Field(default=None, gt=0)
    current_soc: float | None = Field(default=None, ge=0, le=100)


class ChargePointPatch(BaseModel):
    """Only what we own. Vendor, model and firmware come from the charger."""

    label: str | None = None
    heartbeat_interval: int | None = Field(default=None, gt=0)
    registration_status: str | None = None
    supports_charging_profiles: bool | None = None
    require_card_before_start: bool | None = None
    response_delay_s: int | None = Field(default=None, ge=0, le=120)


class ChargePointCreate(BaseModel):
    """Provision a charger before it has ever connected -- real hardware you
    are about to install, or a simulated one, provisioned exactly the same
    way. Only identity is required; everything else gets a sensible default
    if left out, exactly as if the charger had not told us yet.

    vendor/model/serial_number/firmware_version are ordinarily filled in by
    the charger's own BootNotification and read-only after that -- but before
    it has ever connected there is no contradiction in a human supplying the
    same facts up front, so they can be seeded here too.
    """

    identity: str = Field(min_length=1, max_length=48)
    label: str | None = None
    connector_count: int = Field(2, ge=1, le=8)
    max_power_kw: float = Field(11.0, gt=0)
    heartbeat_interval: int = Field(300, gt=0)
    registration_status: str = "Accepted"
    supports_charging_profiles: bool = True
    require_card_before_start: bool = False
    # Set only by the simulator's own provisioning call, never by a human
    # provisioning real hardware through the dashboard -- see the column's
    # own comment in schema.sql for what this actually gates.
    is_simulated: bool = False
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None


# ---------------------------------------------------------------------------
# Charge points
# ---------------------------------------------------------------------------


@router.post("/charge-points")
async def create_charge_point(request: Request, body: ChargePointCreate) -> dict[str, Any]:
    """Provision a charger before it has ever connected -- a real one, or a
    simulated one that connects the moment it is created. There is exactly
    one way a charge_points row comes to exist through the dashboard: this
    endpoint. The simulator's own "add charger" calls this same route rather
    than keeping a second, disconnected notion of what chargers exist, which
    is what let a simulated charger linger in the database as "known but
    offline" after the simulator process forgot about it on restart."""
    async with _db(request).acquire() as conn:
        existing = await cp_repo.get(conn, body.identity)
    if existing is not None:
        raise HTTPException(409, f"{body.identity} already exists")

    async with _db(request).transaction() as conn:
        await cp_repo.create(
            conn,
            identity=body.identity,
            label=body.label,
            connector_count=body.connector_count,
            max_power_kw=body.max_power_kw,
            is_simulated=body.is_simulated,
        )
        # create() only sets what a charger's row cannot be created without
        # (identity, label, connector shape) -- the rest goes through the
        # same allowlisted update() every settings edit already uses, so
        # there is exactly one place that decides which fields are writable.
        await cp_repo.update(
            conn,
            body.identity,
            {
                "heartbeat_interval": body.heartbeat_interval,
                "registration_status": body.registration_status,
                "supports_charging_profiles": body.supports_charging_profiles,
                "require_card_before_start": body.require_card_before_start,
            },
        )
        # These are ordinarily set only by the charger's own BootNotification,
        # and update() deliberately will not touch them for that reason. At
        # creation time there is no contradiction yet -- nothing has
        # connected to disagree -- so a direct write here is not bypassing
        # that rule, it is the one moment the rule does not apply yet.
        if any(
            (body.vendor, body.model, body.serial_number, body.firmware_version)
        ):
            await conn.execute(
                """
                UPDATE charge_points SET
                    vendor = COALESCE(?, vendor),
                    model = COALESCE(?, model),
                    serial_number = COALESCE(?, serial_number),
                    firmware_version = COALESCE(?, firmware_version)
                WHERE identity = ?
                """,
                (body.vendor, body.model, body.serial_number, body.firmware_version, body.identity),
            )
    return {"identity": body.identity}


@router.delete("/charge-points/{identity}")
async def delete_charge_point(request: Request, identity: str) -> dict[str, Any]:
    """Remove a charger and its connectors. Sessions, faults, transactions and
    other history are kept, not cascaded away -- their charge_point_id is
    rewritten to a frozen "{identity}-DELETED-..." label first, so Sessions
    and Logs still show everything that happened, just clearly marked as
    belonging to a charger that no longer exists. Refused while a session is
    genuinely open, the same protection editing or resetting a charger would
    want, so a charger mid-transaction cannot be deleted out from under it.

    Also refused for a simulated charger while its connection is genuinely
    live. This checks the registry directly -- whether a WebSocket for this
    identity is actually open right now -- rather than trusting a caller to
    say it already closed one. The Simulator page's own remove closes the
    connection first and only then reaches this endpoint, so by the time it
    gets here the check below already passes on its own; a human deleting a
    still-connected simulated charger from the Chargers page is correctly
    refused and pointed at the Simulator page instead, or the simulator
    process would be left holding a live connection for a charger whose row
    no longer exists -- exactly the drift that let a deleted charger
    silently reappear, unmarked, the moment it next reconnected.
    """
    async with _db(request).acquire() as conn:
        cp = await cp_repo.get(conn, identity)
        if (
            cp is not None
            and cp["is_simulated"]
            and request.app.state.registry.is_connected(identity)
        ):
            raise HTTPException(
                409,
                f"{identity} is simulated hardware and still connected -- "
                "take it offline or remove it from the Simulator page, not here",
            )
        if await cp_repo.has_open_session(conn, identity):
            raise HTTPException(
                409, f"{identity} has an open session -- end it before removing the charger"
            )
    async with _db(request).transaction() as conn:
        deleted = await cp_repo.delete(conn, identity)
    if not deleted:
        raise HTTPException(404, f"No such charger: {identity}")

    # Diagnostics files are plain files on disk, not database rows -- delete()
    # already relabels every history table's charge_point_id so a recreated
    # charger of the same name never gets confused with the old one's data.
    # These need the same treatment: renamed, not deleted (the file itself
    # may still be worth having), so the plain "{identity}-" prefix a fresh
    # charger of the same name would be found under is free again.
    frozen = f"DELETED-{identity}-{to_db(utcnow()).replace(':', '').replace('.', '')}"
    prefix = f"{identity}-"
    for path in DIAGNOSTICS_DIR.glob(f"{prefix}*"):
        if path.is_file():
            path.rename(DIAGNOSTICS_DIR / f"{frozen}-{path.name[len(prefix):]}")

    registry = request.app.state.registry
    conn_obj = registry.get(identity)
    if conn_obj is not None:
        # The row is gone; nothing should still be able to reach it via a
        # live connection object either, or a stale reference could keep
        # sending it commands that reference rows which no longer exist.
        registry.remove(identity, conn_obj)
    return {"ok": True, "identity": identity}


@router.get("/charge-points")
async def list_charge_points(request: Request) -> list[dict[str, Any]]:
    async with _db(request).acquire() as conn:
        points = await cp_repo.list_all(conn)
        for cp in points:
            cp["connectors"] = await cp_repo.list_connectors(conn, cp["identity"])
            cp["live"] = request.app.state.registry.is_connected(cp["identity"])
    return points


@router.get("/charge-points/{identity}")
async def get_charge_point(request: Request, identity: str) -> dict[str, Any]:
    async with _db(request).acquire() as conn:
        cp = await cp_repo.get(conn, identity)
        if cp is None:
            raise HTTPException(404, f"No charge point {identity}")
        cp["connectors"] = await cp_repo.list_connectors(conn, identity)
        cp["configuration"] = await cp_repo.get_configuration(conn, identity)
        cp["live"] = request.app.state.registry.is_connected(identity)
    return cp


@router.get("/charge-points/{identity}/uptime")
async def get_uptime_summary(request: Request, identity: str) -> dict[str, Any]:
    """The current streak plus both 24h and 7d reliability percentages,
    always both regardless of which timeline window the dashboard has
    selected.

    is_online comes from the database, not the live in-memory registry: the
    registry is empty on every backend restart until a charger's next
    reconnect, while charge_points.is_online only ever changes through the
    same connect/disconnect hooks that write the event history this feature
    is built from. Reading the registry here would let the two disagree for
    as long as a charger happens to stay quiet after a restart.
    """
    async with _db(request).acquire() as conn:
        cp = await cp_repo.get(conn, identity)
        if cp is None:
            raise HTTPException(404, f"No charge point {identity}")
        return await uptime_repo.uptime_summary(
            conn, identity, is_online_now=bool(cp["is_online"])
        )


@router.get("/charge-points/{identity}/uptime/timeline")
async def get_uptime_timeline(
    request: Request, identity: str, window: str = "48h"
) -> dict[str, Any]:
    if window not in ("24h", "48h", "7d"):
        raise HTTPException(400, "window must be '24h', '48h', or '7d'")
    async with _db(request).acquire() as conn:
        cp = await cp_repo.get(conn, identity)
        if cp is None:
            raise HTTPException(404, f"No charge point {identity}")
        return await uptime_repo.timeline(
            conn, identity, window=window, is_online_now=bool(cp["is_online"])
        )


@router.patch("/charge-points/{identity}")
async def update_charge_point(
    request: Request, identity: str, body: ChargePointPatch
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    for flag in ("supports_charging_profiles", "require_card_before_start"):
        if flag in changes:
            changes[flag] = 1 if changes[flag] else 0
    if "response_delay_s" in changes and changes["response_delay_s"] is not None:
        # No computed ceiling here. WebSocketPingInterval looked like the
        # right number to clamp against and was not: real testing on real
        # hardware showed 2s holding fine and 5s reliably reconnecting, which
        # that key does not predict or explain. Rather than clamp to a
        # formula that has already been shown wrong, store whatever is asked
        # for and let the dashboard's warning do the honest job -- say what
        # is known to work, not enforce a number we cannot actually justify.
        changes["response_delay_s"] = max(0, int(changes["response_delay_s"]))
    async with _db(request).transaction() as conn:
        updated = await cp_repo.update(conn, identity, changes)
    if not updated:
        raise HTTPException(404, "No such charger, or nothing to change")

    # A changed delay has to reach the live connection, or it would not apply
    # until the charger next reconnected.
    if "response_delay_s" in changes:
        conn_obj = request.app.state.registry.get(identity)
        if conn_obj is not None:
            await conn_obj.refresh_response_delay()

    return {"identity": identity, **changes}


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    """Everything the landing page needs, in one round trip."""
    async with _db(request).acquire() as conn:
        connectors = await cp_repo.overview(conn)
        open_sessions = await sessions_repo.list_open(conn)
        daily = await metering_repo.energy_by_day(conn, days=14)
        hourly = await metering_repo.energy_by_hour(conn, hours=48)
    return {
        "connectors": connectors,
        "sessions": open_sessions,
        "energy_by_day": daily,
        "energy_by_hour": hourly,
        "connected": request.app.state.registry.connected_ids,
    }


@router.post("/charge-points/{identity}/configuration")
async def change_configuration(
    request: Request, identity: str, body: ConfigRequest
) -> dict[str, Any]:
    try:
        result = await request.app.state.registry.change_configuration(
            identity, key=body.key, value=body.value
        )
    except CommandError as exc:
        raise _handle(exc) from exc
    if result.get("status") == "Accepted":
        async with _db(request).transaction() as conn:
            await cp_repo.upsert_configuration(
                conn, identity, [{"key": body.key, "value": body.value}]
            )
            # HeartbeatInterval is also what we hand back in every
            # BootNotification reply. If only the charger were updated, the
            # next reboot would quietly hand it the old value again.
            if body.key == "HeartbeatInterval" and body.value.isdigit():
                await cp_repo.update(
                    conn, identity, {"heartbeat_interval": int(body.value)}
                )
    return result


@router.post("/charge-points/{identity}/vehicle")
async def attach_vehicle(
    request: Request, identity: str, body: AttachVehicleRequest
) -> dict[str, Any]:
    """Called by the charger when a car is plugged in."""
    try:
        session_id = await _sessions(request).attach_vehicle(
            identity, body.connector_id, body.vehicle_id
        )
    except SessionError as exc:
        raise _handle(exc) from exc
    except INTEGRITY_ERRORS as exc:
        raise _conflict(exc) from exc
    return {"session_id": session_id, "vehicle_id": body.vehicle_id}


# ---------------------------------------------------------------------------
# Sessions: two commands, nothing else
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(
    request: Request,
    charge_point_id: str | None = None,
    vehicle_id: int | None = None,
    limit: int = Query(100, le=500),
) -> list[dict[str, Any]]:
    async with _db(request).acquire() as conn:
        return await sessions_repo.list_recent(
            conn, charge_point_id=charge_point_id, vehicle_id=vehicle_id, limit=limit
        )


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: int) -> dict[str, Any]:
    async with _db(request).acquire() as conn:
        session = await sessions_repo.get(conn, session_id)
        if session is None:
            raise HTTPException(404, f"No session {session_id}")
        session["transactions"] = await sessions_repo.list_transactions(
            conn, session_id
        )
        session["series"] = await metering_repo.series(conn, session_id)
        session["messages"] = await messages_repo.for_session_window(
            conn,
            charge_point_id=session["charge_point_id"],
            start=session["plugged_in_at"],
            end=session["ended_at"],
        )
    return session


@router.post("/charge-points/{identity}/start")
async def start(request: Request, identity: str, body: StartRequest) -> dict[str, Any]:
    """Begin charging, or resume a held session.

    One verb for both, because to the operator it is the same intent: make
    power flow. The domain routes a start on a held session to
    ClearChargingProfile rather than opening a second transaction.
    """
    try:
        result = await _sessions(request).start(
            identity, body.connector_id, id_tag=body.id_tag
        )
    except (SessionError, CommandError) as exc:
        raise _handle(exc) from exc
    return dataclasses.asdict(result)


@router.post("/sessions/{session_id}/stop")
async def stop(request: Request, session_id: int) -> dict[str, Any]:
    """Hold at zero power.

    The transaction stays open, the meter freezes, and the connector latch
    releases so the car can leave. Unplugging is what ends the session.
    """
    try:
        result = await _sessions(request).pause(session_id)
    except (SessionError, CommandError) as exc:
        raise _handle(exc) from exc
    return dataclasses.asdict(result)


# ---------------------------------------------------------------------------
# Commands
#
# One endpoint per OCPP action, named after it. Each returns the charger's
# answer verbatim: a rejection is information, not an error to be smoothed
# over, and the whole point of this panel is seeing what the hardware says.
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/end")
async def end_session(request: Request, session_id: int) -> dict[str, Any]:
    """Send RemoteStopTransaction and close the session for good.

    Distinct from Stop, which holds the connector at zero and keeps the
    transaction open so it can be resumed. This ends it: the charger closes
    the transaction, the meter reading is final, and starting again would
    open a new one.
    """
    try:
        result = await _sessions(request).end(session_id, reason=StopReason.REMOTE)
    except (SessionError, CommandError) as exc:
        raise _handle(exc) from exc
    return dataclasses.asdict(result)


@router.post("/charge-points/{identity}/diagnostics")
async def get_diagnostics(
    request: Request,
    identity: str,
    location: str | None = Body(None, embed=True),
    start_time: str | None = Body(None, embed=True),
    stop_time: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Ask the charger to upload its logs, to us by default.

    The charger uploads out of band, so the location must be an address the
    charger can reach -- not localhost, which to the charger is itself. When
    the caller sends none we fill in our own LAN IP so the file lands in our
    receiver.
    """
    target = location or f"http://{_own_lan_ip()}:9000/diagnostics"
    try:
        return await request.app.state.registry.get_diagnostics(
            identity, location=target, start_time=start_time, stop_time=stop_time
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/reset")
async def reset(
    request: Request, identity: str, type: str = Body("Soft", embed=True)
) -> dict[str, Any]:
    try:
        return await request.app.state.registry.reset(identity, type=type)
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/get-configuration")
async def get_configuration(request: Request, identity: str) -> dict[str, Any]:
    """Ask the charger for every setting it has, and remember the answer.

    Worth running once against any new charger. Until it has been, the
    configuration table is empty and we are guessing at things we could
    simply have asked -- which charging rate unit it accepts, how high a
    profile stack level may go.
    """
    try:
        result = await request.app.state.registry.get_configuration(identity)
    except CommandError as exc:
        raise _handle(exc) from exc

    entries = result.get("configuration_key") or []
    if entries:
        async with _db(request).transaction() as conn:
            await cp_repo.upsert_configuration(conn, identity, entries)
    return {
        "known": len(entries),
        "unknown": result.get("unknown_key") or [],
        "configuration_key": entries,
    }


@router.post("/charge-points/{identity}/trigger")
async def trigger_message(
    request: Request,
    identity: str,
    requested_message: str = Body(..., embed=True),
    connector_id: int | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Ask the charger to send a message now rather than on its own schedule."""
    try:
        return await request.app.state.registry.trigger_message(
            identity, requested_message=requested_message, connector_id=connector_id
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/clear-cache")
async def clear_cache(request: Request, identity: str) -> dict[str, Any]:
    try:
        return await request.app.state.registry.clear_cache(identity)
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/unlock")
async def unlock_connector(
    request: Request, identity: str, connector_id: int = Body(1, embed=True)
) -> dict[str, Any]:
    try:
        return await request.app.state.registry.unlock_connector(
            identity, connector_id=connector_id
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/get-local-list-version")
async def get_local_list_version(request: Request, identity: str) -> dict[str, Any]:
    try:
        return await request.app.state.registry.get_local_list_version(identity)
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/send-local-list")
async def send_local_list(
    request: Request,
    identity: str,
    list_version: int = Body(..., embed=True),
    update_type: str = Body("Full", embed=True),
    local_authorization_list: list[dict[str, Any]] | None = Body(None, embed=True),
) -> dict[str, Any]:
    """update_type is "Full" or "Differential". An empty list with Full
    clears the charger's offline authorization list entirely."""
    try:
        return await request.app.state.registry.send_local_list(
            identity,
            list_version=list_version,
            update_type=update_type,
            local_authorization_list=local_authorization_list,
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/reserve-now")
async def reserve_now(
    request: Request,
    identity: str,
    connector_id: int = Body(..., embed=True),
    expiry_date: str = Body(..., embed=True),
    id_tag: str = Body(..., embed=True),
    reservation_id: int = Body(..., embed=True),
    parent_id_tag: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """expiry_date is ISO 8601. reservation_id is picked by the caller -- keep
    it unique per charger, since CancelReservation needs it back."""
    try:
        return await request.app.state.registry.reserve_now(
            identity,
            connector_id=connector_id,
            expiry_date=expiry_date,
            id_tag=id_tag,
            reservation_id=reservation_id,
            parent_id_tag=parent_id_tag,
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/cancel-reservation")
async def cancel_reservation(
    request: Request, identity: str, reservation_id: int = Body(..., embed=True)
) -> dict[str, Any]:
    try:
        return await request.app.state.registry.cancel_reservation(
            identity, reservation_id=reservation_id
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/composite-schedule")
async def get_composite_schedule(
    request: Request,
    identity: str,
    connector_id: int = Body(1, embed=True),
    duration: int = Body(3600, embed=True),
    charging_rate_unit: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """What a connector will actually deliver right now, combining every
    charging profile stacked on it -- distinct from asking what any single
    profile we sent requested."""
    try:
        return await request.app.state.registry.get_composite_schedule(
            identity,
            connector_id=connector_id,
            duration=duration,
            charging_rate_unit=charging_rate_unit,
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/update-firmware")
async def update_firmware(
    request: Request,
    identity: str,
    location: str = Body(..., embed=True),
    retrieve_date: str = Body(..., embed=True),
    retries: int | None = Body(None, embed=True),
    retry_interval: int | None = Body(None, embed=True),
) -> dict[str, Any]:
    """retrieve_date is ISO 8601. The reply only confirms receipt of the
    command -- watch FirmwareStatusNotification for actual progress."""
    try:
        return await request.app.state.registry.update_firmware(
            identity,
            location=location,
            retrieve_date=retrieve_date,
            retries=retries,
            retry_interval=retry_interval,
        )
    except CommandError as exc:
        raise _handle(exc) from exc


@router.post("/charge-points/{identity}/data-transfer")
async def data_transfer(
    request: Request,
    identity: str,
    vendor_id: str = Body(..., embed=True),
    message_id: str | None = Body(None, embed=True),
    data: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """A charger that does not recognise vendor_id correctly replies
    UnknownVendorId -- that is a valid answer, not a failure."""
    try:
        return await request.app.state.registry.data_transfer(
            identity, vendor_id=vendor_id, message_id=message_id, data=data
        )
    except CommandError as exc:
        raise _handle(exc) from exc


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


@router.get("/tags")
async def list_tags(request: Request) -> list[dict[str, Any]]:
    async with _db(request).acquire() as conn:
        return await tags_repo.list_all(conn)


@router.post("/tags", status_code=201)
async def create_tag(request: Request, body: TagBody) -> dict[str, Any]:
    try:
        async with _db(request).transaction() as conn:
            await tags_repo.create(
                conn,
                id_tag=body.id_tag,
                status=body.status,
                expiry_date=body.expiry_date,
            )
    except INTEGRITY_ERRORS as exc:
        raise _conflict(exc) from exc
    return {"id_tag": body.id_tag}


@router.patch("/tags/{id_tag}")
async def update_tag(request: Request, id_tag: str, body: TagPatch) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    async with _db(request).transaction() as conn:
        updated = await tags_repo.update(conn, id_tag, changes)
    if not updated:
        raise HTTPException(404, "No such card, or nothing to change")
    return {"id_tag": id_tag, **changes}


@router.delete("/tags/{id_tag}")
async def delete_tag(request: Request, id_tag: str) -> dict[str, Any]:
    async with _db(request).transaction() as conn:
        if await tags_repo.has_active_session(conn, id_tag):
            raise HTTPException(
                409, "That card has a session open. Unplug the car first."
            )
        deleted = await tags_repo.delete(conn, id_tag)
    if not deleted:
        raise HTTPException(404, "No such card")
    return {"id_tag": id_tag, "deleted": True}


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------


@router.get("/vehicles")
async def list_vehicles(request: Request) -> list[dict[str, Any]]:
    """The one list of cars.

    Both the dashboard and the simulator read it, so there is a single answer
    to what exists and where each car currently is.
    """
    async with _db(request).acquire() as conn:
        return await tags_repo.list_vehicles(conn)


@router.post("/vehicles", status_code=201)
async def create_vehicle(request: Request, body: VehicleBody) -> dict[str, Any]:
    try:
        async with _db(request).transaction() as conn:
            vehicle_id = await tags_repo.create_vehicle(conn, **body.model_dump())
    except INTEGRITY_ERRORS as exc:
        raise _conflict(exc) from exc
    return {"id": vehicle_id, "name": body.name}


@router.patch("/vehicles/{vehicle_id}")
async def update_vehicle(
    request: Request, vehicle_id: int, body: VehiclePatch
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    try:
        async with _db(request).transaction() as conn:
            updated = await tags_repo.update_vehicle(conn, vehicle_id, changes)
    except INTEGRITY_ERRORS as exc:
        raise _conflict(exc) from exc
    if not updated:
        raise HTTPException(404, "No such vehicle, or nothing to change")
    return {"id": vehicle_id, **changes}


@router.delete("/vehicles/{vehicle_id}")
async def delete_vehicle(request: Request, vehicle_id: int) -> dict[str, Any]:
    async with _db(request).transaction() as conn:
        if await tags_repo.is_plugged_in(conn, vehicle_id):
            raise HTTPException(409, "That car is plugged in. Unplug it first.")
        deleted = await tags_repo.delete_vehicle(conn, vehicle_id)
    if not deleted:
        raise HTTPException(404, "No such vehicle")
    return {"id": vehicle_id, "deleted": True}


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@router.get("/logs")
async def logs(
    request: Request,
    charge_point_id: str | None = None,
    action: str | None = None,
    direction: str | None = None,
    before_id: int | None = None,
    limit: int = Query(200, le=1000),
) -> list[dict[str, Any]]:
    async with _db(request).acquire() as conn:
        return await messages_repo.recent(
            conn,
            charge_point_id=charge_point_id,
            action=action,
            direction=direction,
            before_id=before_id,
            limit=limit,
        )


@router.get("/faults")
async def list_faults(
    request: Request,
    charge_point_id: str | None = None,
    session_id: int | None = None,
    limit: int = Query(200, le=1000),
) -> list[dict[str, Any]]:
    async with _db(request).acquire() as conn:
        return await faults_repo.list_all(
            conn,
            charge_point_id=charge_point_id,
            session_id=session_id,
            limit=limit,
        )


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    db = _db(request)
    return {
        "status": "ok",
        "schema_version": await db.schema_version(),
        "connected_charge_points": request.app.state.registry.connected_ids,
        "server_time": to_db(utcnow()),
    }