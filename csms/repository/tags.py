"""SQL for RFID cards and vehicles.

A card is a credential and nothing more: a number, whether it works, and when
it stops working. A vehicle is a battery with a name. Both are listed with
where they are currently in use, if anywhere -- joined against any session
that is still genuinely open (including FAULTED, which pauses a session
without closing it), so a card or car does not look free the instant its
session merely faults. The session itself remains the only place both facts
are true together at once: which card authorised it, and which car was
connected.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from ..db.database import now_db
from ..db.enums import AuthorizationStatus

Conn = aiosqlite.Connection


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


async def get(conn: Conn, id_tag: str) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM id_tags WHERE id_tag = ?", (id_tag,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_all(conn: Conn) -> list[dict[str, Any]]:
    """Every card, with where it is currently in use if it is in use
    anywhere -- mirrors list_vehicles exactly, so the dashboard and the
    simulator have one consistent answer for both "which cars/cards exist"
    and "where is this one right now".
    """
    async with conn.execute(
        """
        SELECT t.*,
               s.id           AS session_id,
               s.charge_point_id,
               s.connector_id
          FROM id_tags t
          LEFT JOIN charging_sessions s
                 ON s.id_tag = t.id_tag
                AND s.state IN ('WAITING', 'ACTIVE', 'PAUSED', 'FAULTED')
         ORDER BY t.id_tag
        """
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def has_active_session(conn: Conn, id_tag: str) -> bool:
    """OCPP calls this ConcurrentTx: one card cannot run two transactions.

    A WAITING session is deliberately not counted. It has no transaction --
    it is a cable sitting in a socket -- so a card presented at it is not a
    second concurrent transaction, it is the driver starting the first one.
    Counting it meant presenting a card at a connector that already had a
    session open was refused, which is precisely the normal case.

    FAULTED is counted, unlike WAITING: a fault is not terminal, and the
    underlying transaction very likely stays genuinely open on the real
    charger's side straight through it, so the card is still legitimately in
    use even though the session's clock is paused.
    """
    async with conn.execute(
        """
        SELECT 1 FROM charging_sessions
         WHERE id_tag = ? AND state IN ('ACTIVE', 'PAUSED', 'FAULTED')
         LIMIT 1
        """,
        (id_tag,),
    ) as cur:
        return await cur.fetchone() is not None


async def expire_if_due(conn: Conn, id_tag: str) -> None:
    """Flip an Accepted-but-past-expiry card to Expired, so the stored status
    matches what we return. Keeps the card list honest without a cron job."""
    await conn.execute(
        """
        UPDATE id_tags SET status = ?
         WHERE id_tag = ? AND status = ?
           AND expiry_date IS NOT NULL AND expiry_date <= ?
        """,
        (
            AuthorizationStatus.EXPIRED.value,
            id_tag,
            AuthorizationStatus.ACCEPTED.value,
            now_db(),
        ),
    )


async def create(
    conn: Conn,
    *,
    id_tag: str,
    status: str = AuthorizationStatus.ACCEPTED.value,
    expiry_date: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO id_tags (id_tag, status, expiry_date) VALUES (?, ?, ?)",
        (id_tag, status, expiry_date),
    )


async def update(conn: Conn, id_tag: str, changes: dict[str, Any]) -> bool:
    allowed = {"status", "expiry_date"}
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return False
    assignments = ", ".join(f"{k} = ?" for k in fields)
    async with conn.execute(
        f"UPDATE id_tags SET {assignments} WHERE id_tag = ?",
        (*fields.values(), id_tag),
    ) as cur:
        return cur.rowcount > 0


async def delete(conn: Conn, id_tag: str) -> bool:
    async with conn.execute("DELETE FROM id_tags WHERE id_tag = ?", (id_tag,)) as cur:
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------


async def get_vehicle(conn: Conn, vehicle_id: int) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_vehicles(conn: Conn) -> list[dict[str, Any]]:
    """Every car, with where it is plugged in if it is plugged in anywhere.

    The dashboard and the simulator both read this, so there is exactly one
    answer to "which cars exist" and "where is this one".
    """
    async with conn.execute(
        """
        SELECT v.*,
               s.id           AS session_id,
               s.charge_point_id,
               s.connector_id,
               s.state        AS session_state
          FROM vehicles v
          LEFT JOIN charging_sessions s
                 ON s.vehicle_id = v.id
                AND s.state IN ('WAITING', 'ACTIVE', 'PAUSED', 'FAULTED')
         ORDER BY v.name
        """
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def create_vehicle(
    conn: Conn,
    *,
    name: str,
    battery_capacity_kwh: float,
    max_charge_kw: float = 11.0,
    current_soc: float = 20.0,
) -> int:
    async with conn.execute(
        """
        INSERT INTO vehicles (name, battery_capacity_kwh, max_charge_kw, current_soc)
        VALUES (?, ?, ?, ?)
        """,
        (name, battery_capacity_kwh, max_charge_kw, current_soc),
    ) as cur:
        return int(cur.lastrowid or 0)


async def update_vehicle(conn: Conn, vehicle_id: int, changes: dict[str, Any]) -> bool:
    allowed = {"name", "battery_capacity_kwh", "max_charge_kw", "current_soc"}
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return False
    assignments = ", ".join(f"{k} = ?" for k in fields)
    async with conn.execute(
        f"UPDATE vehicles SET {assignments} WHERE id = ?",
        (*fields.values(), vehicle_id),
    ) as cur:
        return cur.rowcount > 0


async def delete_vehicle(conn: Conn, vehicle_id: int) -> bool:
    async with conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,)) as cur:
        return cur.rowcount > 0


async def set_soc(conn: Conn, vehicle_id: int, soc: float) -> None:
    await conn.execute(
        "UPDATE vehicles SET current_soc = MAX(0.0, MIN(100.0, ?)) WHERE id = ?",
        (soc, vehicle_id),
    )


async def is_plugged_in(conn: Conn, vehicle_id: int) -> dict[str, Any] | None:
    """The open session this car is already part of, if any.

    A car cannot be in two sockets at once. A partial unique index enforces it
    in storage; this gives the API something useful to say when it happens.
    """
    async with conn.execute(
        """
        SELECT id, charge_point_id, connector_id FROM charging_sessions
         WHERE vehicle_id = ? AND state IN ('WAITING', 'ACTIVE', 'PAUSED', 'FAULTED')
         LIMIT 1
        """,
        (vehicle_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None