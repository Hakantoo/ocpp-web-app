"""SQL for meter_values: ingest and the queries behind the charts."""

from __future__ import annotations

from typing import Any

import aiosqlite

from ..db.database import now_db
from ..db.enums import Measurand

Conn = aiosqlite.Connection

# Anything outside the schema's CHECK list is dropped rather than failing the
# whole MeterValues message: one unrecognised measurand should not cost us the
# rest of the sample.
KNOWN_MEASURANDS = {m.value for m in Measurand}


async def insert_samples(conn: Conn, samples: list[dict[str, Any]]) -> int:
    """Bulk-insert sampled values. Returns how many were actually stored."""
    rows = [
        (
            s.get("transaction_id"),
            s.get("session_id"),
            s["charge_point_id"],
            s["connector_id"],
            s["timestamp"],
            s["measurand"],
            float(s["value"]),
            s.get("unit"),
            s.get("phase"),
            s.get("context"),
        )
        for s in samples
        if s.get("measurand") in KNOWN_MEASURANDS
    ]
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO meter_values
            (transaction_id, session_id, charge_point_id, connector_id, timestamp,
             measurand, value, unit, phase, context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


async def series(
    conn: Conn,
    session_id: int,
    measurands: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """All samples for one session, grouped by measurand.

    Shaped for the charts: {"Power.Active.Import": [{"t": ..., "v": ...}, ...]}
    """
    measurands = measurands or [
        Measurand.ENERGY_ACTIVE_IMPORT_REGISTER.value,
        Measurand.POWER_ACTIVE_IMPORT.value,
        Measurand.SOC.value,
    ]
    placeholders = ", ".join("?" * len(measurands))
    async with conn.execute(
        f"""
        SELECT measurand, timestamp, value, unit
          FROM meter_values
         WHERE session_id = ? AND measurand IN ({placeholders})
         ORDER BY timestamp
        """,
        (session_id, *measurands),
    ) as cur:
        rows = await cur.fetchall()

    out: dict[str, list[dict[str, Any]]] = {m: [] for m in measurands}
    for r in rows:
        out[r["measurand"]].append(
            {"t": r["timestamp"], "v": r["value"], "unit": r["unit"]}
        )
    return out


async def latest(
    conn: Conn, session_id: int, measurand: str
) -> dict[str, Any] | None:
    async with conn.execute(
        """
        SELECT timestamp, value, unit FROM meter_values
         WHERE session_id = ? AND measurand = ?
         ORDER BY timestamp DESC LIMIT 1
        """,
        (session_id, measurand),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def energy_by_hour(
    conn: Conn, charge_point_id: str | None = None, hours: int = 48
) -> list[dict[str, Any]]:
    """Energy delivered per clock hour, computed from the meter itself.

    The obvious shortcut -- grouping sessions by their start time -- puts an
    overnight session's entire output into the hour it began, which is useless
    for seeing when load actually lands. Instead we difference consecutive
    readings of the cumulative register with LAG() and attribute each delta to
    the hour it was measured in.

    MAX(delta, 0) discards any negative step, which is what a meter reset or a
    charger swap looks like in the data.
    """
    where = "AND m.charge_point_id = ?" if charge_point_id else ""
    params: list[Any] = [f"-{int(hours)} hours"]
    if charge_point_id:
        params.append(charge_point_id)
    params.append(hours)

    async with conn.execute(
        f"""
        WITH readings AS (
            SELECT
                m.timestamp,
                m.value,
                LAG(m.value) OVER (
                    PARTITION BY m.transaction_id ORDER BY m.timestamp
                ) AS previous
            FROM meter_values m
            WHERE m.measurand = 'Energy.Active.Import.Register'
              AND m.transaction_id IS NOT NULL
              AND m.timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
              {where}
        )
        SELECT substr(timestamp, 1, 13) AS hour,
               SUM(MAX(value - previous, 0)) / 1000.0 AS kwh
          FROM readings
         WHERE previous IS NOT NULL
         GROUP BY hour
         ORDER BY hour DESC
         LIMIT ?
        """,
        params,
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def energy_by_day(
    conn: Conn, charge_point_id: str | None = None, days: int = 30
) -> list[dict[str, Any]]:
    """Daily kWh totals, for the overview chart.

    Grouped on the date prefix of the ISO timestamp, which is exact because
    every timestamp is stored in UTC with the same format.

    Today's date is always present in the result, even as 0 kWh with no
    sessions, because the caller's "Today" figure reads the first row and
    assumes it genuinely is today. A plain GROUP BY only returns days that
    had a session -- on a day with no charging yet, that silently left the
    most recent day *with* data sitting in the first slot, mislabeled as
    today's total when it could be several days stale.
    """
    where = "WHERE charge_point_id = ?" if charge_point_id else ""
    params: list[Any] = [charge_point_id] if charge_point_id else []
    params.append(days)
    async with conn.execute(
        f"""
        SELECT substr(COALESCE(started_at, plugged_in_at), 1, 10) AS day,
               SUM(energy_wh) / 1000.0 AS kwh,
               COUNT(*) AS sessions
          FROM charging_sessions
        {where}
         GROUP BY day
         ORDER BY day DESC
         LIMIT ?
        """,
        params,
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    today = now_db()[:10]
    if not rows or rows[0]["day"] != today:
        rows.insert(0, {"day": today, "kwh": 0.0, "sessions": 0})
    return rows