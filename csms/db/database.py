"""Async SQLite access layer.

Deliberately thin: a connection pool, a transaction context manager, and four
fetch helpers. Everything above this file writes plain SQL.

Why a pool rather than one shared connection
--------------------------------------------
aiosqlite runs each connection on its own thread. With a single shared
connection, two concurrent tasks doing ``async with db.transaction()`` would
interleave their BEGIN/COMMIT statements on the same connection and silently
merge into one transaction. A pool gives every task its own connection, so
transaction boundaries mean what they say. WAL mode allows any number of
concurrent readers alongside one writer, and ``busy_timeout`` makes a
contended write wait instead of raising "database is locked".

Pragmas
-------
* ``journal_mode=WAL``   dashboard reads no longer block on MeterValues writes
* ``foreign_keys=ON``    SQLite ignores foreign keys entirely by default
* ``synchronous=NORMAL`` the right trade-off with WAL: durable across process
                         crashes, only at risk from an OS-level crash
* ``busy_timeout``       wait rather than fail on write contention
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 12

Params = Sequence[Any] | Mapping[str, Any] | None


# ---------------------------------------------------------------------------
# Timestamp helpers
#
# One format everywhere: ISO-8601, UTC, millisecond precision, 'Z' suffix.
# Lexicographic order equals chronological order, so SQL comparisons on the
# raw column are correct.
# ---------------------------------------------------------------------------

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_db(value: datetime | None) -> str | None:
    """datetime -> the canonical TEXT representation."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(TS_FORMAT)[:-3] + "Z"


def now_db() -> str:
    return to_db(utcnow())  # type: ignore[return-value]


def from_db(value: str | None) -> datetime | None:
    """The canonical TEXT representation -> aware datetime."""
    if value is None:
        return None
    text = value.rstrip("Z")
    if "." not in text:
        text += ".000"
    return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)


def dumps(value: Any) -> str | None:
    """JSON-encode a payload for a TEXT column."""
    return None if value is None else json.dumps(value, separators=(",", ":"))


def loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class Database:
    """A small pool of aiosqlite connections to one database file."""

    def __init__(
        self,
        path: str | Path,
        *,
        pool_size: int = 5,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.pool_size = pool_size
        self.busy_timeout_ms = busy_timeout_ms
        self._pool: asyncio.LifoQueue[aiosqlite.Connection] = asyncio.LifoQueue()
        self._all: list[aiosqlite.Connection] = []
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        if self._started:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(self.pool_size):
            conn = await self._new_connection()
            self._all.append(conn)
            self._pool.put_nowait(conn)
        self._started = True

    async def close(self) -> None:
        for conn in self._all:
            await conn.close()
        self._all.clear()
        self._pool = asyncio.LifoQueue()
        self._started = False

    async def _new_connection(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        # aiosqlite.Connection is a Thread. Left non-daemonic, an unhandled
        # exception that skips close() leaves the interpreter hanging at exit
        # forever waiting to join these threads. Daemonic threads let the
        # process die, which is what we want from a crashed server.
        conn.daemon = True
        conn.row_factory = sqlite3.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

    # -- schema ------------------------------------------------------------

    async def initialise(self) -> None:
        """Apply schema.sql, then any column migrations.

        Idempotent: table creation is IF NOT EXISTS, views are dropped and
        recreated so their definitions are always current, and added columns
        are checked against PRAGMA table_info before the ALTER runs. An
        existing database upgrades in place -- no re-seed needed.
        """
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        async with self.acquire() as conn:
            # Migrations run first, before the views are recreated: SQLite
            # refuses to drop a column that a live view still references.
            await self._migrate(conn)
            await conn.executescript(sql)
            current = await conn.execute_fetchall(
                "SELECT MAX(version) AS v FROM schema_version"
            )
            if current[0]["v"] is None or int(current[0]["v"]) < SCHEMA_VERSION:
                await conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """Bring an older database up to the current shape, in place.

        Each step is guarded, so running this against a fresh file or an
        already-current one is a no-op. Nothing here destroys charging history.
        """
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cur:
            tables = {row["name"] for row in await cur.fetchall()}

        async def columns_of(table: str) -> set[str]:
            if table not in tables:
                return set()
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                return {row["name"] for row in await cur.fetchall()}

        # Views reference columns we are about to drop.
        await conn.execute("DROP VIEW IF EXISTS v_active_sessions")
        await conn.execute("DROP VIEW IF EXISTS v_connector_overview")

        # -- added columns --------------------------------------------------
        added: list[tuple[str, str, str]] = [
            ("charging_sessions", "active_since", "TEXT"),
            ("connectors", "authorized_id_tag", "TEXT"),
            (
                "charge_points",
                "response_delay_s",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "charge_points",
                "require_card_before_start",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            ("charge_points", "diagnostics_status", "TEXT"),
            (
                "charge_points",
                "is_simulated",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "charge_points",
                "is_tombstone",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "vehicles",
                "charge_profile",
                "TEXT NOT NULL DEFAULT 'generic'",
            ),
        ]
        for table, column, ddl in added:
            if table in tables and column not in await columns_of(table):
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

        # -- removed columns ------------------------------------------------
        # Cards no longer carry a holder or a car; those were the wrong place
        # to look for either. Vehicles no longer carry a target charge level.
        await conn.execute("DROP INDEX IF EXISTS ix_id_tags_user")

        removed: list[tuple[str, str]] = [
            ("id_tags", "user_id"),
            ("id_tags", "parent_id_tag"),
            ("id_tags", "label"),
            ("id_tags", "note"),
            ("vehicles", "target_soc"),
            ("charge_points", "autostart_policy"),
            ("charge_points", "requires_replug_after_stop"),
        ]
        for table, column in removed:
            if table in tables and column in await columns_of(table):
                try:
                    await conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                except Exception:  # noqa: BLE001 - pinned by an index
                    log.warning("Could not drop %s.%s", table, column)

        # vehicles.id_tag carries a UNIQUE constraint, whose automatic index
        # SQLite will not let us drop. The only way out is the documented
        # rebuild: copy the surviving columns into a new table and swap it in.
        if "vehicles" in tables and "id_tag" in await columns_of("vehicles"):
            log.info("Rebuilding vehicles to drop its card link")
            await conn.execute("PRAGMA foreign_keys=OFF")
            await conn.execute(
                """
                CREATE TABLE vehicles_rebuilt (
                    id                    INTEGER PRIMARY KEY,
                    name                  TEXT NOT NULL,
                    battery_capacity_kwh  REAL NOT NULL CHECK (battery_capacity_kwh > 0),
                    max_charge_kw         REAL NOT NULL DEFAULT 11.0 CHECK (max_charge_kw > 0),
                    current_soc           REAL NOT NULL DEFAULT 20.0
                                              CHECK (current_soc BETWEEN 0 AND 100),
                    created_at            TEXT NOT NULL
                                              DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO vehicles_rebuilt
                    (id, name, battery_capacity_kwh, max_charge_kw, current_soc, created_at)
                SELECT id, name, battery_capacity_kwh, max_charge_kw, current_soc, created_at
                  FROM vehicles
                """
            )
            await conn.execute("DROP TABLE vehicles")
            await conn.execute("ALTER TABLE vehicles_rebuilt RENAME TO vehicles")
            await conn.execute("PRAGMA foreign_keys=ON")

        # -- removed tables --------------------------------------------------
        for table in ("session_schedules", "users"):
            await conn.execute(f"DROP TABLE IF EXISTS {table}")

    async def schema_version(self) -> int | None:
        return await self.fetch_value("SELECT MAX(version) FROM schema_version")

    # -- connections -------------------------------------------------------

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        """Borrow a connection from the pool and return it afterwards."""
        if not self._started:
            await self.connect()
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put_nowait(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Explicit transaction. Commits on success, rolls back on exception.

        BEGIN IMMEDIATE takes the write lock up front rather than at the first
        write, which turns a possible mid-transaction SQLITE_BUSY into a clean
        wait governed by busy_timeout.
        """
        async with self.acquire() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")

    # -- queries -----------------------------------------------------------

    async def fetch_all(self, sql: str, params: Params = None) -> list[sqlite3.Row]:
        async with self.acquire() as conn:
            async with conn.execute(sql, params or ()) as cursor:
                return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: Params = None) -> sqlite3.Row | None:
        async with self.acquire() as conn:
            async with conn.execute(sql, params or ()) as cursor:
                return await cursor.fetchone()

    async def fetch_value(self, sql: str, params: Params = None) -> Any:
        row = await self.fetch_one(sql, params)
        return None if row is None else row[0]

    async def execute(self, sql: str, params: Params = None) -> int:
        """Run one statement outside an explicit transaction.

        Returns ``lastrowid`` for INSERT, ``rowcount`` otherwise.
        """
        async with self.acquire() as conn:
            async with conn.execute(sql, params or ()) as cursor:
                return cursor.lastrowid if cursor.lastrowid else cursor.rowcount

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        async with self.acquire() as conn:
            await conn.executemany(sql, rows)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


__all__ = [
    "Database",
    "Params",
    "SCHEMA_VERSION",
    "dumps",
    "from_db",
    "loads",
    "now_db",
    "row_to_dict",
    "rows_to_dicts",
    "to_db",
    "utcnow",
]