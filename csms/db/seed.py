"""Create the schema and load a minimal working fixture.

    python -m csms.db.seed          # create + seed (safe to re-run)
    python -m csms.db.seed --reset  # delete the database file first
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from ..config import settings
from .database import Database, now_db, to_db, utcnow
from .enums import AuthorizationStatus, ConnectorStatus

CHARGE_POINT_ID = "CP001"

# Mirrors what the simulator reports from GetConfiguration.
DEFAULT_CONFIG: list[tuple[str, str, int]] = [
    ("HeartbeatInterval", "300", 0),
    ("MeterValueSampleInterval", "10", 0),
    (
        "MeterValuesSampledData",
        "Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage,SoC",
        0,
    ),
    ("ConnectionTimeOut", "60", 0),
    ("NumberOfConnectors", "2", 1),
    ("SupportedFeatureProfiles", "Core,SmartCharging,RemoteTrigger", 1),
    ("ChargeProfileMaxStackLevel", "10", 1),
    ("ChargingScheduleAllowedChargingRateUnit", "Current,Power", 1),
    ("WebSocketPingInterval", "60", 0),
]


async def seed(db: Database) -> None:
    await db.initialise()

    existing = await db.fetch_value(
        "SELECT 1 FROM charge_points WHERE identity = ?", (CHARGE_POINT_ID,)
    )
    if existing:
        print(f"{CHARGE_POINT_ID} already present; nothing to do.")
        return

    now = utcnow()

    async with db.transaction() as conn:
        # -- cards ----------------------------------------------------------
        # Two that work, two that are deliberately broken, so the
        # authorisation path can be exercised without editing the database.
        await conn.executemany(
            "INSERT INTO id_tags (id_tag, status, expiry_date) VALUES (?, ?, ?)",
            [
                (
                    "RFID-0001",
                    AuthorizationStatus.ACCEPTED.value,
                    to_db(now + timedelta(days=365)),
                ),
                ("RFID-0002", AuthorizationStatus.ACCEPTED.value, None),
                ("RFID-BLOCKED", AuthorizationStatus.BLOCKED.value, None),
                (
                    "RFID-EXPIRED",
                    AuthorizationStatus.ACCEPTED.value,
                    to_db(now - timedelta(days=1)),
                ),
            ],
        )

        # -- vehicles -------------------------------------------------------
        await conn.executemany(
            """
            INSERT INTO vehicles (name, battery_capacity_kwh, max_charge_kw, current_soc)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("Kona Electric", 64.0, 11.0, 24.0),
                ("ID.4 Pro", 77.0, 11.0, 52.0),
                ("Model 3", 60.0, 11.0, 68.0),
            ],
        )

        # -- charge point ---------------------------------------------------
        await conn.execute(
            """
            INSERT INTO charge_points
                (identity, label, vendor, model, serial_number, firmware_version, is_simulated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                CHARGE_POINT_ID,
                "Garage charger",
                "SimVendor",
                "SimAC22",
                "SIM-0001",
                "1.0.0",
                1,
            ),
        )

        # Connector 0 represents the charge point itself, per OCPP 1.6.
        await conn.executemany(
            """
            INSERT INTO connectors
                (charge_point_id, connector_id, status, max_power_kw, status_updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (CHARGE_POINT_ID, 0, ConnectorStatus.AVAILABLE.value, None, now_db()),
                (CHARGE_POINT_ID, 1, ConnectorStatus.AVAILABLE.value, 11.0, now_db()),
                (CHARGE_POINT_ID, 2, ConnectorStatus.AVAILABLE.value, 11.0, now_db()),
            ],
        )

        await conn.executemany(
            """
            INSERT INTO configuration_keys (charge_point_id, key, value, readonly)
            VALUES (?, ?, ?, ?)
            """,
            [(CHARGE_POINT_ID, k, v, ro) for k, v, ro in DEFAULT_CONFIG],
        )

    print(
        f"Seeded {CHARGE_POINT_ID}: 2 connectors, 4 cards, 3 vehicles, "
        f"{len(DEFAULT_CONFIG)} configuration keys."
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Initialise the CSMS database.")
    parser.add_argument(
        "--reset", action="store_true", help="delete the database file first"
    )
    args = parser.parse_args()

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            path = settings.database_path.with_name(settings.database_path.name + suffix)
            path.unlink(missing_ok=True)
        print("Removed existing database.")

    db = Database(
        settings.database_path,
        pool_size=settings.db_pool_size,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    try:
        await db.connect()
        await seed(db)
        print(f"Database at {settings.database_path} (schema v{await db.schema_version()})")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())