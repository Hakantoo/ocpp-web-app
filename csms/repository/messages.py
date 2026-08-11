"""SQL for the OCPP message log.

Writes here are on the hot path of every frame, so they are kept to a single
INSERT with no reads.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from ..db.database import dumps, now_db
from ..db.enums import MessageDirection

Conn = aiosqlite.Connection


async def log(
    conn: Conn,
    *,
    charge_point_id: str | None,
    direction: MessageDirection | str,
    message_type_id: int,
    unique_id: str | None = None,
    action: str | None = None,
    payload: Any = None,
    error_code: str | None = None,
    error_description: str | None = None,
    error_details: Any = None,
    session_id: int | None = None,
) -> int:
    async with conn.execute(
        """
        INSERT INTO message_log
            (charge_point_id, direction, message_type_id, unique_id, action, payload,
             error_code, error_description, error_details, session_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            charge_point_id,
            str(direction),
            message_type_id,
            # The spec caps the unique message ID at 36 characters; a charger
            # that sends more would otherwise trip the CHECK constraint and
            # take down the logging path with it.
            (unique_id or None) and unique_id[:36],
            action,
            dumps(payload),
            error_code,
            error_description,
            dumps(error_details),
            session_id,
            now_db(),
        ),
    ) as cur:
        return int(cur.lastrowid or 0)


async def recent(
    conn: Conn,
    *,
    charge_point_id: str | None = None,
    action: str | None = None,
    direction: str | None = None,
    limit: int = 200,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if charge_point_id:
        clauses.append("charge_point_id = ?")
        params.append(charge_point_id)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if direction:
        clauses.append("direction = ?")
        params.append(direction)
    if before_id:
        clauses.append("id < ?")
        params.append(before_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    async with conn.execute(
        f"SELECT * FROM message_log {where} ORDER BY id DESC LIMIT ?", params
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def for_session_window(
    conn: Conn,
    *,
    charge_point_id: str,
    start: str,
    end: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Frames exchanged with one charger during one session's lifetime.

    Correlating by time window rather than by a session_id column is
    deliberate: a frame is logged the instant it arrives, before any handler
    has decided which session it belongs to. Stamping the column would mean
    either logging late (and losing frames that crash a handler) or updating
    rows afterwards. Timestamps are fixed-width ISO-8601 UTC, so the range
    comparison is exact.
    """
    async with conn.execute(
        """
        SELECT * FROM message_log
         WHERE charge_point_id = ?
           AND timestamp >= ?
           AND (? IS NULL OR timestamp <= ?)
         ORDER BY id
         LIMIT ?
        """,
        (charge_point_id, start, end, end, limit),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def prune(conn: Conn, keep: int = 50_000) -> int:
    """Drop the oldest rows beyond `keep`. The log is the fastest-growing
    table and nothing downstream depends on old frames."""
    async with conn.execute(
        "DELETE FROM message_log WHERE id <= "
        "(SELECT MAX(id) FROM message_log) - ?",
        (keep,),
    ) as cur:
        return cur.rowcount
