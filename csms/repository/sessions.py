"""SQL for charging sessions, OCPP transactions and charging profiles.

These three live together because they are always manipulated as one unit:
starting a session creates a transaction, pausing one writes a profile row,
and every state change touches at least two of the three tables.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from ..db.database import now_db
from ..db.enums import SessionState, StopReason, TransactionState

Conn = aiosqlite.Connection

OPEN_STATES = ("WAITING", "ACTIVE", "PAUSED", "FAULTED")
_OPEN_PLACEHOLDERS = ", ".join("?" * len(OPEN_STATES))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


# Charging time that ticks without a background writer, plus the connector
# latch state. Both are derived, so they can never drift from `state`.
LIVE_COLUMNS = """
    active_seconds + CASE
        WHEN active_since IS NULL THEN 0
        ELSE CAST((julianday('now') - julianday(replace(active_since, 'Z', ''))) * 86400 AS INTEGER)
    END AS active_seconds_live,
    CASE WHEN state = 'ACTIVE' THEN 1 ELSE 0 END AS cable_locked
"""


async def get(conn: Conn, session_id: int) -> dict[str, Any] | None:
    """One session, with everything the detail page shows.

    The vehicle is joined here rather than left to the caller because the list
    query already does it, and two screens reading the same record should not
    disagree about whether it has a car attached.
    """
    async with conn.execute(
        f"""
        SELECT s.*, {LIVE_COLUMNS},
               v.name AS vehicle_name,
               v.battery_capacity_kwh,
               v.max_charge_kw,
               v.current_soc,
               cp.label AS charge_point_label
          FROM charging_sessions s
          LEFT JOIN vehicles v ON v.id = s.vehicle_id
          LEFT JOIN charge_points cp ON cp.identity = s.charge_point_id
         WHERE s.id = ?
        """,
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_open_on_connector(
    conn: Conn, charge_point_id: str, connector_id: int
) -> dict[str, Any] | None:
    """The one open session on this connector, if any.

    A partial unique index guarantees there is at most one, so this cannot
    silently pick an arbitrary row.
    """
    async with conn.execute(
        f"""
        SELECT * FROM charging_sessions
         WHERE charge_point_id = ? AND connector_id = ?
           AND state IN ({_OPEN_PLACEHOLDERS})
        """,
        (charge_point_id, connector_id, *OPEN_STATES),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_open(conn: Conn) -> list[dict[str, Any]]:
    async with conn.execute("SELECT * FROM v_active_sessions") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def list_recent(
    conn: Conn,
    *,
    charge_point_id: str | None = None,
    vehicle_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if charge_point_id:
        clauses.append("s.charge_point_id = ?")
        params.append(charge_point_id)
    if vehicle_id:
        clauses.append("s.vehicle_id = ?")
        params.append(vehicle_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    async with conn.execute(
        f"""
        SELECT s.*,
               s.active_seconds + CASE
                   WHEN s.active_since IS NULL THEN 0
                   ELSE CAST((julianday('now') - julianday(replace(s.active_since, 'Z', ''))) * 86400 AS INTEGER)
               END AS active_seconds_live,
               CASE WHEN s.state = 'ACTIVE' THEN 1 ELSE 0 END AS cable_locked,
               v.name AS vehicle_name,
               v.battery_capacity_kwh,
               cp.label AS charge_point_label
          FROM charging_sessions s
          LEFT JOIN vehicles v ON v.id = s.vehicle_id
          LEFT JOIN charge_points cp ON cp.identity = s.charge_point_id
        {where}
         ORDER BY COALESCE(s.started_at, s.plugged_in_at) DESC
         LIMIT ?
        """,
        params,
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def create(
    conn: Conn,
    *,
    charge_point_id: str,
    connector_pk: int,
    connector_id: int,
    id_tag: str | None = None,
    vehicle_id: int | None = None,
    state: SessionState = SessionState.WAITING,
) -> int:
    async with conn.execute(
        """
        INSERT INTO charging_sessions
            (charge_point_id, connector_pk, connector_id, id_tag, vehicle_id,
             state, plugged_in_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            charge_point_id,
            connector_pk,
            connector_id,
            id_tag,
            vehicle_id,
            str(state),
            now_db(),
        ),
    ) as cur:
        return int(cur.lastrowid or 0)


async def _enter_active(conn: Conn, session_id: int) -> None:
    """Start the charging-time clock. COALESCE keeps the original start if the
    session is already running, so a repeated call cannot reset it."""
    await conn.execute(
        "UPDATE charging_sessions SET active_since = COALESCE(active_since, ?) WHERE id = ?",
        (now_db(), session_id),
    )


async def _leave_active(conn: Conn, session_id: int) -> None:
    """Stop the clock and bank the elapsed time.

    Doing the arithmetic in SQL rather than in Python means the value is
    computed against the same clock that wrote active_since, and a crash
    between reading and writing cannot lose the interval.
    """
    await conn.execute(
        """
        UPDATE charging_sessions
           SET active_seconds = active_seconds + CASE
                   WHEN active_since IS NULL THEN 0
                   ELSE CAST((julianday('now') - julianday(replace(active_since, 'Z', ''))) * 86400 AS INTEGER)
               END,
               active_since = NULL
         WHERE id = ?
        """,
        (session_id,),
    )


async def pause_clock(conn: Conn, session_id: int) -> None:
    """Stop counting charging time without changing the session's state.

    Used when the car stops drawing (SuspendedEV). We did not pause anything,
    so the session is still ACTIVE and the transaction is still open -- but no
    energy is flowing, and charging time is supposed to measure energy
    actually being delivered.
    """
    await _leave_active(conn, session_id)


async def resume_clock(conn: Conn, session_id: int) -> None:
    """Start counting again when the car resumes drawing."""
    await _enter_active(conn, session_id)


async def set_state(
    conn: Conn, session_id: int, state: SessionState, *, start_clock: bool = True
) -> None:
    """Change state and keep the charging-time clock consistent with it.

    Every transition in or out of ACTIVE goes through here, so there is no way
    to change state and forget the clock. start_clock=False lets a caller
    move to ACTIVE without assuming power is already flowing -- resume() uses
    this, since clearing a pause profile is not the same fact as the charger
    having actually resumed delivery yet.
    """
    await conn.execute(
        "UPDATE charging_sessions SET state = ? WHERE id = ?",
        (str(state), session_id),
    )
    if state is SessionState.ACTIVE and start_clock:
        await _enter_active(conn, session_id)
    elif state is not SessionState.ACTIVE:
        await _leave_active(conn, session_id)


async def mark_started(
    conn: Conn,
    session_id: int,
    *,
    id_tag: str | None = None,
    vehicle_id: int | None = None,
    currently_charging: bool = False,
) -> None:
    """WAITING -> ACTIVE. started_at is set only once, on the first Start.

    The clock only starts if currently_charging is true -- a transaction
    opening is not the same fact as power actually flowing. A card swipe
    opens the transaction immediately but can land the connector at
    SuspendedEV first, with the real Charging status notification arriving
    as a separate message afterward.
    """
    await conn.execute(
        """
        UPDATE charging_sessions
           SET state = ?,
               started_at = COALESCE(started_at, ?),
               id_tag = COALESCE(?, id_tag),
               vehicle_id = COALESCE(?, vehicle_id)
         WHERE id = ?
        """,
        (SessionState.ACTIVE.value, now_db(), id_tag, vehicle_id, session_id),
    )
    if currently_charging:
        await _enter_active(conn, session_id)
    else:
        await _leave_active(conn, session_id)


async def mark_completed(
    conn: Conn,
    session_id: int,
    *,
    reason: StopReason | str | None = None,
) -> None:
    # Bank the final interval first; after this the session is closed and the
    # clock must never run again.
    await _leave_active(conn, session_id)
    await conn.execute(
        """
        UPDATE charging_sessions
           SET state = ?, ended_at = ?, end_reason = COALESCE(?, end_reason)
         WHERE id = ? AND state != 'COMPLETED'
        """,
        (
            SessionState.COMPLETED.value,
            now_db(),
            str(reason) if reason else None,
            session_id,
        ),
    )


async def refresh_energy(conn: Conn, session_id: int) -> int:
    """Recompute energy_wh as the sum of its transactions' deltas.

    Cheap (one or two rows) and it removes any chance of the denormalised
    total drifting away from the underlying meter readings.
    """
    await conn.execute(
        """
        UPDATE charging_sessions
           SET energy_wh = (
               SELECT COALESCE(SUM(
                   COALESCE(meter_stop_wh, meter_last_wh, meter_start_wh) - meter_start_wh
               ), 0)
                 FROM transactions WHERE session_id = ?
           )
         WHERE id = ?
        """,
        (session_id, session_id),
    )
    async with conn.execute(
        "SELECT energy_wh FROM charging_sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    return int(row["energy_wh"]) if row else 0


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


async def next_transaction_id(conn: Conn) -> int:
    """Allocate the integer the charger sees in StartTransaction.conf.

    Monotonic and never reused, so the same ID appearing twice in a log is
    always a charger bug rather than ours.
    """
    async with conn.execute(
        "SELECT COALESCE(MAX(ocpp_transaction_id), 0) + 1 AS n FROM transactions"
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"])


async def create_transaction(
    conn: Conn,
    *,
    ocpp_transaction_id: int,
    session_id: int,
    charge_point_id: str,
    connector_id: int,
    id_tag: str | None,
    meter_start_wh: int,
    started_at: str | None = None,
    reservation_id: int | None = None,
) -> int:
    async with conn.execute(
        """
        INSERT INTO transactions
            (ocpp_transaction_id, session_id, charge_point_id, connector_id, id_tag,
             meter_start_wh, meter_last_wh, started_at, reservation_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ocpp_transaction_id,
            session_id,
            charge_point_id,
            connector_id,
            id_tag,
            meter_start_wh,
            meter_start_wh,
            started_at or now_db(),
            reservation_id,
        ),
    ) as cur:
        return int(cur.lastrowid or 0)


async def get_transaction_by_ocpp_id(
    conn: Conn, ocpp_transaction_id: int
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM transactions WHERE ocpp_transaction_id = ?",
        (ocpp_transaction_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_active_transaction(conn: Conn, session_id: int) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM transactions WHERE session_id = ? AND state = ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id, TransactionState.ACTIVE.value),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_transactions(conn: Conn, session_id: int) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT * FROM transactions WHERE session_id = ? ORDER BY id", (session_id,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_meter_last(conn: Conn, transaction_id: int, meter_wh: int) -> None:
    """Advance the live register reading.

    MAX() guards against a charger reporting a value lower than one we have
    already stored; the schema's CHECK would otherwise reject the write and
    abort the whole MeterValues transaction.
    """
    await conn.execute(
        "UPDATE transactions SET meter_last_wh = MAX(COALESCE(meter_last_wh, 0), ?) "
        "WHERE id = ?",
        (meter_wh, transaction_id),
    )


async def stop_transaction(
    conn: Conn,
    transaction_id: int,
    *,
    meter_stop_wh: int | None = None,
    reason: StopReason | str | None = None,
    stopped_at: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE transactions
           SET state = ?,
               stopped_at = ?,
               meter_stop_wh = MAX(
                   COALESCE(?, meter_last_wh, meter_start_wh), meter_start_wh),
               stop_reason = ?
         WHERE id = ? AND state = ?
        """,
        (
            TransactionState.STOPPED.value,
            stopped_at or now_db(),
            meter_stop_wh,
            str(reason) if reason else None,
            transaction_id,
            TransactionState.ACTIVE.value,
        ),
    )


async def stop_active_transaction(
    conn: Conn, session_id: int, *, reason: StopReason | str | None = None
) -> dict[str, Any] | None:
    tx = await get_active_transaction(conn, session_id)
    if tx:
        await stop_transaction(conn, int(tx["id"]), reason=reason)
    return tx


async def close_all_open_for_charge_point(
    conn: Conn, charge_point_id: str, reason: StopReason
) -> list[int]:
    """Close every open session on a charger. Used after a hard reset or a
    boot that invalidates whatever we thought was happening."""
    async with conn.execute(
        f"SELECT id FROM charging_sessions WHERE charge_point_id = ? "
        f"AND state IN ({_OPEN_PLACEHOLDERS})",
        (charge_point_id, *OPEN_STATES),
    ) as cur:
        ids = [int(r["id"]) for r in await cur.fetchall()]

    for session_id in ids:
        await stop_active_transaction(conn, session_id, reason=reason)
        await refresh_energy(conn, session_id)
        await mark_completed(conn, session_id, reason=reason)
    return ids


# ---------------------------------------------------------------------------
# Charging profiles -- how pause is implemented
# ---------------------------------------------------------------------------


async def record_profile(
    conn: Conn,
    *,
    charge_point_id: str,
    connector_id: int,
    session_id: int | None,
    ocpp_profile_id: int,
    purpose: str,
    stack_level: int,
    limit_value: float,
    limit_unit: str = "W",
) -> int:
    async with conn.execute(
        """
        INSERT INTO charging_profiles
            (charge_point_id, connector_id, session_id, ocpp_profile_id, purpose,
             stack_level, limit_value, limit_unit, applied_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            charge_point_id,
            connector_id,
            session_id,
            ocpp_profile_id,
            purpose,
            stack_level,
            limit_value,
            limit_unit,
            now_db(),
        ),
    ) as cur:
        return int(cur.lastrowid or 0)


async def get_active_pause_profile(
    conn: Conn, charge_point_id: str, connector_id: int
) -> dict[str, Any] | None:
    """The 0 W profile currently holding a connector paused, if any."""
    async with conn.execute(
        """
        SELECT * FROM charging_profiles
         WHERE charge_point_id = ? AND connector_id = ?
           AND cleared_at IS NULL AND limit_value = 0
         ORDER BY id DESC LIMIT 1
        """,
        (charge_point_id, connector_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def clear_profile(conn: Conn, profile_row_id: int) -> None:
    await conn.execute(
        "UPDATE charging_profiles SET cleared_at = ? WHERE id = ? AND cleared_at IS NULL",
        (now_db(), profile_row_id),
    )


async def clear_profiles_for_connector(
    conn: Conn, charge_point_id: str, connector_id: int
) -> None:
    await conn.execute(
        """
        UPDATE charging_profiles SET cleared_at = ?
         WHERE charge_point_id = ? AND connector_id = ? AND cleared_at IS NULL
        """,
        (now_db(), charge_point_id, connector_id),
    )


async def set_vehicle(conn: Conn, session_id: int, vehicle_id: int | None) -> None:
    """Bind a car to a session.

    Done at plug-in rather than at StartTransaction, so a session that is
    merely waiting still knows which car is sitting on it.
    """
    await conn.execute(
        "UPDATE charging_sessions SET vehicle_id = ? WHERE id = ?",
        (vehicle_id, session_id),
    )