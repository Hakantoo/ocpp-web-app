"""SQL for charge points, connectors and configuration keys.

Every function takes an open aiosqlite connection as its first argument.
Callers decide the transaction boundary; the repository never opens one.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from ..db.database import now_db
from ..db.enums import ChargePointErrorCode, ConnectorStatus, RegistrationStatus

Conn = aiosqlite.Connection


async def get(conn: Conn, identity: str) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM charge_points WHERE identity = ?", (identity,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def exists(conn: Conn, identity: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM charge_points WHERE identity = ?", (identity,)
    ) as cur:
        return await cur.fetchone() is not None


async def list_all(conn: Conn) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT * FROM charge_points WHERE is_tombstone = 0 ORDER BY identity"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def register_unknown(conn: Conn, identity: str) -> None:
    """Insert a charge point we have never seen, plus connector 0.

    Used when reject_unknown_charge_points is off, so a brand new charger can
    connect and appear in the dashboard without manual provisioning.

    The registration status is Accepted rather than Pending on purpose. Pending
    is the spec's onboarding handshake: the charger waits, the CSMS configures
    it, and only then sends Accepted. We do not run that flow, so a charger
    left Pending would connect, boot, and then sit there refusing to start
    transactions with nothing ever coming to release it.
    """
    await conn.execute(
        """
        INSERT OR IGNORE INTO charge_points (identity, label, registration_status)
        VALUES (?, ?, ?)
        """,
        (identity, f"Unprovisioned {identity}", RegistrationStatus.ACCEPTED.value),
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO connectors (charge_point_id, connector_id, status_updated_at)
        VALUES (?, 0, ?)
        """,
        (identity, now_db()),
    )


async def apply_boot_notification(
    conn: Conn, identity: str, payload: dict[str, Any]
) -> None:
    """Record the identity fields a charger reports in BootNotification."""
    await conn.execute(
        """
        UPDATE charge_points SET
            vendor              = COALESCE(?, vendor),
            model               = COALESCE(?, model),
            serial_number       = COALESCE(?, serial_number),
            firmware_version    = COALESCE(?, firmware_version),
            iccid               = COALESCE(?, iccid),
            imsi                = COALESCE(?, imsi),
            meter_type          = COALESCE(?, meter_type),
            meter_serial_number = COALESCE(?, meter_serial_number),
            last_boot_at        = ?,
            last_seen           = ?
        WHERE identity = ?
        """,
        (
            payload.get("charge_point_vendor"),
            payload.get("charge_point_model"),
            payload.get("charge_point_serial_number")
            or payload.get("charge_box_serial_number"),
            payload.get("firmware_version"),
            payload.get("iccid"),
            payload.get("imsi"),
            payload.get("meter_type"),
            payload.get("meter_serial_number"),
            now_db(),
            now_db(),
            identity,
        ),
    )


async def set_online(conn: Conn, identity: str, online: bool) -> None:
    await conn.execute(
        "UPDATE charge_points SET is_online = ?, last_seen = ? WHERE identity = ?",
        (1 if online else 0, now_db(), identity),
    )


async def touch(conn: Conn, identity: str) -> None:
    """Bump last_seen. Called on Heartbeat and on any inbound message."""
    await conn.execute(
        "UPDATE charge_points SET last_seen = ? WHERE identity = ?",
        (now_db(), identity),
    )


async def set_registration_status(
    conn: Conn, identity: str, status: RegistrationStatus
) -> None:
    await conn.execute(
        "UPDATE charge_points SET registration_status = ? WHERE identity = ?",
        (status.value, identity),
    )


async def set_diagnostics_status(conn: Conn, identity: str, status: str) -> None:
    await conn.execute(
        "UPDATE charge_points SET diagnostics_status = ? WHERE identity = ?",
        (status, identity),
    )


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------


async def count_physical_connectors(conn: Conn, identity: str) -> int:
    """How many sockets we know about. Connector 0 is the unit, not a socket."""
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM connectors "
        "WHERE charge_point_id = ? AND connector_id > 0",
        (identity,),
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def get_connector(
    conn: Conn, identity: str, connector_id: int
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM connectors WHERE charge_point_id = ? AND connector_id = ?",
        (identity, connector_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_connectors(conn: Conn, identity: str) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT * FROM connectors WHERE charge_point_id = ? ORDER BY connector_id",
        (identity,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def ensure_connector(conn: Conn, identity: str, connector_id: int) -> int:
    """Return the connector's surrogate key, creating the row if needed.

    A charger may report a StatusNotification for a connector we do not know
    about yet -- for example the first time an unprovisioned unit connects.
    """
    await conn.execute(
        """
        INSERT OR IGNORE INTO connectors (charge_point_id, connector_id, status_updated_at)
        VALUES (?, ?, ?)
        """,
        (identity, connector_id, now_db()),
    )
    async with conn.execute(
        "SELECT id FROM connectors WHERE charge_point_id = ? AND connector_id = ?",
        (identity, connector_id),
    ) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update_connector_status(
    conn: Conn,
    identity: str,
    connector_id: int,
    status: ConnectorStatus | str,
    error_code: ChargePointErrorCode | str = ChargePointErrorCode.NO_ERROR,
    info: str | None = None,
    vendor_error_code: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE connectors
           SET status = ?, error_code = ?, info = ?, vendor_error_code = ?,
               status_updated_at = ?
         WHERE charge_point_id = ? AND connector_id = ?
        """,
        (
            str(status),
            str(error_code),
            info,
            vendor_error_code,
            now_db(),
            identity,
            connector_id,
        ),
    )


async def set_authorization(
    conn: Conn, identity: str, connector_id: int, id_tag: str | None
) -> None:
    """Record which card was presented at a socket, or clear it.

    Authorize carries only the card number -- OCPP does not say which socket
    it was presented at. The caller (connection.py's on_authorize) works out
    a specific connector_id whenever it genuinely can, by checking which
    connector is actually waiting for a card. connector_id=0 here means the
    caller could not narrow it down -- no connector waiting, or more than one
    at once -- and this falls back to setting it across every connector on
    the unit, since there is no better answer available in that case. This
    fallback used to be the *only* behaviour; it is now deliberately the
    exception, not the default.
    """
    if connector_id:
        await conn.execute(
            "UPDATE connectors SET authorized_id_tag = ? "
            "WHERE charge_point_id = ? AND connector_id = ?",
            (id_tag, identity, connector_id),
        )
    else:
        await conn.execute(
            "UPDATE connectors SET authorized_id_tag = ? "
            "WHERE charge_point_id = ? AND connector_id > 0",
            (id_tag, identity),
        )


async def get_authorization(
    conn: Conn, identity: str, connector_id: int
) -> str | None:
    async with conn.execute(
        "SELECT authorized_id_tag FROM connectors "
        "WHERE charge_point_id = ? AND connector_id = ?",
        (identity, connector_id),
    ) as cur:
        row = await cur.fetchone()
    return row["authorized_id_tag"] if row else None


async def charging_profile_limits(conn: Conn, identity: str) -> dict[str, Any]:
    """What kind of charging profile this charger will actually accept.

    Two details decide whether a profile is taken or rejected, and both differ
    between units:

    * the rate unit -- AC chargers commonly accept amps only, and we were
      sending watts
    * the stack level -- a profile above the charger's maximum is invalid

    Both are ordinary configuration keys, so once GetConfiguration has been
    run we can read them instead of guessing. Defaults are the conservative
    reading: amps, and the lowest stack level.
    """
    async with conn.execute(
        """
        SELECT key, value FROM configuration_keys
         WHERE charge_point_id = ?
           AND key IN ('ChargingScheduleAllowedChargingRateUnit',
                       'ChargeProfileMaxStackLevel')
        """,
        (identity,),
    ) as cur:
        keys = {row["key"]: (row["value"] or "") for row in await cur.fetchall()}

    units = keys.get("ChargingScheduleAllowedChargingRateUnit", "")
    if units:
        # The key is a comma separated list, and chargers spell it either
        # "Current"/"Power" or "A"/"W".
        allowed = {u.strip().lower() for u in units.split(",")}
        if allowed & {"power", "w"}:
            unit = "W"
        else:
            unit = "A"
    else:
        unit = None  # unknown: the caller decides what to try first

    raw_level = keys.get("ChargeProfileMaxStackLevel", "")
    try:
        max_stack = int(raw_level) if raw_level else None
    except ValueError:
        max_stack = None

    return {"unit": unit, "max_stack_level": max_stack}


async def mark_all_connectors_unavailable(conn: Conn, identity: str) -> None:
    """Called when a charge point drops off the network.

    The charger's real connector states are unknown while it is offline;
    showing the last known state would be a lie, so they are marked
    Unavailable until it reports again.
    """
    await conn.execute(
        """
        UPDATE connectors SET status = ?, status_updated_at = ?
         WHERE charge_point_id = ? AND status != ?
        """,
        (
            ConnectorStatus.UNAVAILABLE.value,
            now_db(),
            identity,
            ConnectorStatus.UNAVAILABLE.value,
        ),
    )


async def overview(conn: Conn) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT * FROM v_connector_overview ORDER BY charge_point_id, connector_id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Configuration keys
# ---------------------------------------------------------------------------


async def get_configuration(conn: Conn, identity: str) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT key, value, readonly FROM configuration_keys "
        "WHERE charge_point_id = ? ORDER BY key",
        (identity,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def get_configuration_value(
    conn: Conn, identity: str, key: str
) -> str | None:
    async with conn.execute(
        "SELECT value FROM configuration_keys WHERE charge_point_id = ? AND key = ?",
        (identity, key),
    ) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


async def upsert_configuration(
    conn: Conn, identity: str, entries: list[dict[str, Any]]
) -> None:
    await conn.executemany(
        """
        INSERT INTO configuration_keys (charge_point_id, key, value, readonly, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (charge_point_id, key)
        DO UPDATE SET value = excluded.value,
                      readonly = excluded.readonly,
                      updated_at = excluded.updated_at
        """,
        [
            (
                identity,
                e["key"],
                e.get("value"),
                1 if e.get("readonly") else 0,
                now_db(),
            )
            for e in entries
        ],
    )


# ---------------------------------------------------------------------------
# Writes from the dashboard
# ---------------------------------------------------------------------------


async def create(
    conn: Conn,
    *,
    identity: str,
    label: str | None = None,
    connector_count: int = 2,
    max_power_kw: float | None = 11.0,
    is_simulated: bool = False,
) -> None:
    """Provision a charger before it has ever connected.

    Connector 0 represents the unit as a whole and is always created; physical
    sockets are numbered from 1. is_simulated marks a row as the simulator's
    own fake hardware -- set only by the simulator's own provisioning
    endpoint, never for a charger a human is provisioning through the
    dashboard for real hardware, since that flag is what tells the simulator,
    on its own restart, which rows are safe to reconnect to.
    """
    await conn.execute(
        "INSERT INTO charge_points (identity, label, is_simulated) VALUES (?, ?, ?)",
        (identity, label, 1 if is_simulated else 0),
    )
    rows = [(identity, 0, now_db(), None)]
    rows += [
        (identity, n, now_db(), max_power_kw) for n in range(1, connector_count + 1)
    ]
    await conn.executemany(
        """
        INSERT INTO connectors (charge_point_id, connector_id, status_updated_at, max_power_kw)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )



async def update(conn: Conn, identity: str, changes: dict[str, Any]) -> bool:
    """Only the two settings we actually own are writable.

    Vendor, model, serial and firmware are excluded on purpose: the charger
    reports those in BootNotification, and letting the dashboard overwrite
    them would mean the record no longer describes the hardware.
    """
    allowed = {
        "label",
        "heartbeat_interval",
        "supports_charging_profiles",
        # Editable because a charger left Pending refuses to work and there
        # would otherwise be no way to release it short of editing the
        # database by hand.
        "registration_status",
        "require_card_before_start",
        "response_delay_s",
    }
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return False
    assignments = ", ".join(f"{k} = ?" for k in fields)
    async with conn.execute(
        f"UPDATE charge_points SET {assignments} WHERE identity = ?",
        (*fields.values(), identity),
    ) as cur:
        return cur.rowcount > 0


async def delete(conn: Conn, identity: str) -> bool:
    """Removes only the charger and its connectors -- not its history.

    Every table that references this charger for historical reasons
    (sessions, transactions, faults, connection events, configuration keys,
    and the raw OCPP message log) has its charge_point_id rewritten first, to
    a frozen label that can never collide with a real charger's identity
    again, so a plain DELETE on charge_points does not cascade any of that
    away.

    charging_sessions has a second cascade path that is easy to miss:
    connector_pk references connectors(id) ON DELETE CASCADE too, so
    relabeling charge_point_id alone is not enough -- deleting the real
    connectors would still cascade-delete every session through that column.
    Sessions are repointed at a placeholder connector under the tombstone
    charger first, closing that path before the real connectors go.

    Connectors themselves are the one thing that does not survive: a
    connector only makes sense attached to a charger that still exists, so
    those are deleted along with the charger, same as before.

    The foreign keys mean the frozen label and the placeholder connector
    both have to point at something real, so a tiny tombstone charger row
    (and one placeholder connector under it) are inserted first. is_tombstone
    marks the charger row so list queries can exclude it; neither is ever
    meant to be seen as real hardware.
    """
    frozen = f"{identity}-DELETED-{now_db().replace(':', '').replace('.', '')}"
    await conn.execute(
        "INSERT INTO charge_points (identity, label, is_tombstone) VALUES (?, ?, 1)",
        (frozen, f"{identity} (deleted)"),
    )
    placeholder_connector_pk = await ensure_connector(conn, frozen, 0)

    await conn.execute(
        "UPDATE charging_sessions SET connector_pk = ? WHERE charge_point_id = ?",
        (placeholder_connector_pk, identity),
    )
    history_tables = (
        "charging_sessions",
        "transactions",
        "faults",
        "connection_events",
        "configuration_keys",
        # No foreign key forces this one -- message_log.charge_point_id is
        # plain TEXT -- but the session detail page correlates frames to a
        # session by matching this string against the session's own
        # charge_point_id. Leaving message_log un-relabeled while
        # charging_sessions gets relabeled breaks that match silently: the
        # frames are still there, just no longer found.
        "message_log",
    )
    for table in history_tables:
        await conn.execute(
            f"UPDATE {table} SET charge_point_id = ? WHERE charge_point_id = ?",
            (frozen, identity),
        )
    async with conn.execute(
        "DELETE FROM charge_points WHERE identity = ?", (identity,)
    ) as cur:
        return cur.rowcount > 0


async def has_open_session(conn: Conn, identity: str) -> bool:
    async with conn.execute(
        """
        SELECT 1 FROM charging_sessions
         WHERE charge_point_id = ? AND state IN ('WAITING', 'ACTIVE', 'PAUSED', 'FAULTED')
         LIMIT 1
        """,
        (identity,),
    ) as cur:
        return await cur.fetchone() is not None


async def fault_all_connectors(
    conn: Conn, identity: str, error_code: str
) -> list[int]:
    """A fault reported on connector 0 means the whole unit is down.

    OCPP allows a charger to report a fault against connector 0 rather than a
    specific socket. Leaving the individual connectors showing Available while
    the unit is faulted would be actively misleading, so they all follow.
    """
    await conn.execute(
        """
        UPDATE connectors
           SET status = ?, error_code = ?, status_updated_at = ?
         WHERE charge_point_id = ? AND connector_id > 0
        """,
        (ConnectorStatus.FAULTED.value, error_code, now_db(), identity),
    )
    async with conn.execute(
        "SELECT connector_id FROM connectors WHERE charge_point_id = ? AND connector_id > 0",
        (identity,),
    ) as cur:
        return [int(r["connector_id"]) for r in await cur.fetchall()]