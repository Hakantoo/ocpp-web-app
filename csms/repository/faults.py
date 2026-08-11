"""Fault occurrences: one row per Faulted status, independent of sessions.

A fault no longer ends a session or its transaction -- the charger frequently
keeps both running straight through the fault window, and closing our side
just to reopen it moments later was the bug this table exists to stop
recreating. What we need instead is purely historical: when did a connector
fault, what was it, and when (if ever) did it clear.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from ..db.database import now_db

Conn = aiosqlite.Connection


async def open_fault(
    conn: Conn,
    *,
    charge_point_id: str,
    connector_id: int,
    error_code: str | None,
    vendor_error_code: str | None,
    info: str | None,
    session_id: int | None,
) -> int:
    """Record a new Faulted occurrence. Returns the new row's id.

    The unique index on (charge_point_id, connector_id) WHERE cleared_at IS
    NULL means a second Faulted before the first clears is rejected by SQLite
    rather than silently duplicated -- callers should check for an already-open
    fault first if that matters to them.
    """
    cur = await conn.execute(
        """
        INSERT INTO faults
            (charge_point_id, connector_id, error_code, vendor_error_code,
             info, session_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (charge_point_id, connector_id, error_code, vendor_error_code, info,
         session_id),
    )
    return int(cur.lastrowid)


async def get_open_fault(
    conn: Conn, charge_point_id: str, connector_id: int
) -> dict[str, Any] | None:
    async with conn.execute(
        """
        SELECT * FROM faults
         WHERE charge_point_id = ? AND connector_id = ? AND cleared_at IS NULL
        """,
        (charge_point_id, connector_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def clear_fault(conn: Conn, fault_id: int) -> None:
    await conn.execute(
        "UPDATE faults SET cleared_at = ? WHERE id = ? AND cleared_at IS NULL",
        (now_db(), fault_id),
    )


async def clear_open_fault(
    conn: Conn, charge_point_id: str, connector_id: int
) -> None:
    """Close whatever fault is open on this connector, if any. A no-op when
    there was none -- most status transitions call this defensively."""
    await conn.execute(
        """
        UPDATE faults SET cleared_at = ?
         WHERE charge_point_id = ? AND connector_id = ? AND cleared_at IS NULL
        """,
        (now_db(), charge_point_id, connector_id),
    )


_LIST_BASE = """
    SELECT
        f.id, f.charge_point_id, f.connector_id, f.session_id,
        f.error_code, f.vendor_error_code, f.info,
        f.occurred_at, f.cleared_at,
        cp.label AS charge_point_label
      FROM faults f
      LEFT JOIN charge_points cp ON cp.identity = f.charge_point_id
"""


async def list_all(
    conn: Conn,
    *,
    charge_point_id: str | None = None,
    session_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Global or filtered fault history, newest first.

    Powers both call sites: the Protocol Log's faults table (optionally
    filtered by charger) and a session's own faults table (filtered by
    session_id, never by charger).
    """
    where: list[str] = []
    params: list[Any] = []
    if charge_point_id:
        where.append("f.charge_point_id = ?")
        params.append(charge_point_id)
    if session_id is not None:
        where.append("f.session_id = ?")
        params.append(session_id)

    sql = _LIST_BASE
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY f.occurred_at DESC LIMIT ?"
    params.append(limit)

    async with conn.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]