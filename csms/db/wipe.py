"""Clear charging history without losing your setup.

    python -m csms.db.wipe                 # sessions, transactions, readings
    python -m csms.db.wipe --logs          # also the OCPP frame log
    python -m csms.db.wipe --all           # everything above, plus reset SoC
    python -m csms.db.wipe --logs-only     # just the frame log
    python -m csms.db.wipe --yes           # skip the confirmation prompt

Cards, vehicles, chargers, connectors and configuration keys are never
touched. To start completely fresh instead, use ``python -m csms.db.seed
--reset``, which deletes the database file.

Why this exists rather than a DELETE in the sqlite3 CLI: SQLite disables
foreign keys by default, and the CLI does not turn them on. Deleting sessions
there leaves orphaned transactions and meter readings behind, because the
ON DELETE CASCADE rules never fire. This tool connects with foreign keys
enabled, so one delete takes its dependents with it.
"""

from __future__ import annotations

import argparse
import asyncio

from ..config import settings
from .database import Database

# Deleting a session cascades to its transactions, meter readings and charging
# profiles. message_log is deliberately not linked by a foreign key -- frames
# are recorded the instant they arrive, before any handler knows which session
# they belong to -- so it is cleared separately and only when asked.
CASCADED = ("charging_sessions", "transactions", "meter_values", "charging_profiles")


async def counts(db: Database, tables: tuple[str, ...]) -> dict[str, int]:
    return {t: await db.fetch_value(f"SELECT COUNT(*) FROM {t}") or 0 for t in tables}


async def wipe(
    db: Database, *, logs: bool, reset_soc: bool, logs_only: bool
) -> dict[str, int]:
    tables = ("message_log",) if logs_only else CASCADED + (
        ("message_log",) if logs else ()
    )
    before = await counts(db, tables)

    async with db.transaction() as conn:
        if not logs_only:
            # One delete is enough: the cascade takes transactions, meter
            # values and charging profiles with it.
            await conn.execute("DELETE FROM charging_sessions")
        if logs or logs_only:
            await conn.execute("DELETE FROM message_log")
        if reset_soc:
            await conn.execute("UPDATE vehicles SET current_soc = 20.0")

    # Ids restart at 1 on their own: these tables use a plain INTEGER PRIMARY
    # KEY rather than AUTOINCREMENT, so SQLite picks the highest existing
    # rowid plus one -- and once the table is empty, that is 1.
    return before


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear charging history, keeping cards, cars and chargers."
    )
    parser.add_argument("--logs", action="store_true", help="also clear the frame log")
    parser.add_argument(
        "--logs-only", action="store_true", help="clear only the frame log"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="clear history and the frame log, and reset every car to 20%%",
    )
    parser.add_argument("--yes", action="store_true", help="do not ask")
    args = parser.parse_args()

    logs = args.logs or args.all
    db = Database(
        settings.database_path,
        pool_size=settings.db_pool_size,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    try:
        await db.connect()

        open_sessions = await db.fetch_value(
            "SELECT COUNT(*) FROM charging_sessions "
            "WHERE state IN ('WAITING', 'ACTIVE', 'PAUSED', 'FAULTED')"
        )
        if open_sessions and not args.logs_only:
            print(
                f"Warning: {open_sessions} session(s) are still open. Unplug first, "
                "or the dashboard and the charger will disagree about what exists."
            )

        if not args.yes:
            what = "the frame log" if args.logs_only else "charging history"
            answer = input(f"Delete {what} from {settings.database_path}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing was deleted.")
                return

        before = await wipe(
            db, logs=logs, reset_soc=args.all, logs_only=args.logs_only
        )
        for table, count in before.items():
            print(f"  cleared {count:>7} rows from {table}")
        if args.all:
            print("  reset every vehicle to 20%")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())