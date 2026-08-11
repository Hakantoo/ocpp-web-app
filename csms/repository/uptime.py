"""Connection uptime, reconstructed from connect/disconnect events.

Three things live here, all derived from the same raw event log:

* the current streak -- how long the charger has been up, or how long it has
  been down, whichever is true right now
* a timeline of segments (connected/disconnected, with a start and end) for a
  given window, drawn as a strip in the dashboard
* a reliability percentage -- what fraction of a window was actually spent
  connected

Collection starts the moment this ships. There is no way to backfill history
that was never recorded, so a charger added yesterday has a real but short
history, and one added five minutes ago has almost none -- both are shown
plainly rather than padded with anything invented.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import aiosqlite

from ..db.database import now_db

Conn = aiosqlite.Connection

EventName = Literal["connected", "disconnected"]


async def record(conn: Conn, charge_point_id: str, event: EventName) -> None:
    await conn.execute(
        "INSERT INTO connection_events (charge_point_id, event) VALUES (?, ?)",
        (charge_point_id, event),
    )


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


async def timeline(
    conn: Conn,
    charge_point_id: str,
    *,
    window: Literal["24h", "48h", "7d"],
    is_online_now: bool,
) -> dict[str, Any]:
    """Segments covering the requested window, and the reliability percentage
    for that same window.

    A stretch of the window with no event at all before it -- because the
    charger's history simply had not started yet -- is its own "unknown"
    segment, shown as such rather than guessed at. Counting unknown time as
    disconnected would unfairly tank the percentage for a charger that is
    only new to having history collected; counting it as connected would be
    just as invented in the other direction. So it is excluded from the
    percentage's denominator entirely, and shown distinctly in the strip.
    """
    now = datetime.now(timezone.utc)
    span = {
        "24h": timedelta(hours=24),
        "48h": timedelta(hours=48),
        "7d": timedelta(days=7),
    }[window]
    start = now - span

    async with conn.execute(
        """
        SELECT event, occurred_at FROM connection_events
         WHERE charge_point_id = ? AND occurred_at >= ?
         ORDER BY occurred_at ASC
        """,
        (charge_point_id, _fmt(start)),
    ) as cur:
        in_window = [dict(r) for r in await cur.fetchall()]

    async with conn.execute(
        """
        SELECT event, occurred_at FROM connection_events
         WHERE charge_point_id = ? AND occurred_at < ?
         ORDER BY occurred_at DESC LIMIT 1
        """,
        (charge_point_id, _fmt(start)),
    ) as cur:
        row = await cur.fetchone()
    before = dict(row) if row else None

    if not in_window and before is None:
        # No history at all before or during the window: nothing is known.
        segments = [{"start": _fmt(start), "end": _fmt(now), "connected": None}]
    elif not in_window:
        # History exists further back, none of it fell inside this window --
        # the state has simply held since before it opened.
        state = before["event"] == "connected"
        segments = [{"start": _fmt(start), "end": _fmt(now), "connected": state}]
    else:
        segments = []
        cursor = start
        if before is not None:
            state: bool | None = before["event"] == "connected"
        else:
            # First-ever event happens to fall inside this window: everything
            # before it is genuinely unknown, not assumed.
            first_ts = _parse(in_window[0]["occurred_at"])
            if first_ts > start:
                segments.append(
                    {"start": _fmt(start), "end": _fmt(first_ts), "connected": None}
                )
                cursor = first_ts
            state = None

        for ev in in_window:
            ts = _parse(ev["occurred_at"])
            if ts > cursor:
                segments.append(
                    {"start": _fmt(cursor), "end": _fmt(ts), "connected": state}
                )
            state = ev["event"] == "connected"
            cursor = max(cursor, ts)
        if cursor < now:
            segments.append(
                {"start": _fmt(cursor), "end": _fmt(now), "connected": state}
            )

    total_known = sum(
        (_parse(s["end"]) - _parse(s["start"])).total_seconds()
        for s in segments
        if s["connected"] is not None
    )
    connected = sum(
        (_parse(s["end"]) - _parse(s["start"])).total_seconds()
        for s in segments
        if s["connected"] is True
    )
    percent = round((connected / total_known) * 100, 1) if total_known > 0 else None

    return {"window": window, "segments": segments, "percent": percent}


async def current_streak(
    conn: Conn, charge_point_id: str, *, is_online_now: bool
) -> dict[str, Any]:
    """How long the charger has been in its current state, connected or not.

    Reads the single most recent event; if there has never been one (a
    charger that has never actually connected since this table existed),
    there is nothing to report and the caller shows "no history yet" rather
    than a fabricated duration.
    """
    async with conn.execute(
        """
        SELECT event, occurred_at FROM connection_events
         WHERE charge_point_id = ?
         ORDER BY occurred_at DESC LIMIT 1
        """,
        (charge_point_id,),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        return {"since": None, "seconds": None, "connected": is_online_now}

    since = _parse(row["occurred_at"])
    now = datetime.now(timezone.utc)
    seconds = int((now - since).total_seconds())
    return {
        "since": row["occurred_at"],
        "seconds": max(0, seconds),
        "connected": is_online_now,
    }


async def uptime_summary(conn: Conn, charge_point_id: str, *, is_online_now: bool) -> dict[str, Any]:
    """Everything the dashboard needs in one call: the current streak plus
    both 48h and 7d percentages, always both regardless of which panel is
    being looked at."""
    streak = await current_streak(conn, charge_point_id, is_online_now=is_online_now)
    two_day = await timeline(conn, charge_point_id, window="48h", is_online_now=is_online_now)
    week = await timeline(conn, charge_point_id, window="7d", is_online_now=is_online_now)
    return {
        "streak": streak,
        "percent_48h": two_day["percent"],
        "percent_7d": week["percent"],
    }