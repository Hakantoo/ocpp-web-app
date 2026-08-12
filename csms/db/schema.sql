-- ===========================================================================
-- CSMS schema (OCPP 1.6J)
--
-- Conventions
--   * Timestamps are TEXT, ISO-8601, always UTC, always with a 'Z' suffix.
--     Lexicographic order == chronological order, so BETWEEN and ORDER BY
--     work directly on the column.
--   * Booleans are INTEGER 0/1 with a CHECK constraint.
--   * Enumerations are TEXT with a CHECK constraint. Values taken from the
--     OCPP 1.6 specification keep the exact casing used on the wire so that
--     no translation is needed when serialising. Values that are ours alone
--     are UPPER_SNAKE, which makes the distinction obvious at a glance.
--   * Energy is stored in Wh as INTEGER, exactly as OCPP reports it. Only the
--     presentation layer converts to kWh.
-- ===========================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- ===========================================================================
-- Identity
-- ===========================================================================

-- An RFID card. Just a credential: a number, whether it is usable, and when
-- it stops being usable. It deliberately says nothing about who holds it or
-- what they drive -- those are separate concerns and tying them here made the
-- card the wrong place to look for both.
CREATE TABLE IF NOT EXISTS id_tags (
    id_tag       TEXT PRIMARY KEY CHECK (length(id_tag) BETWEEN 1 AND 20),
    status       TEXT NOT NULL DEFAULT 'Accepted'
                     CHECK (status IN ('Accepted', 'Blocked', 'Expired',
                                       'Invalid', 'ConcurrentTx')),
    expiry_date  TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- A car. Its state of charge persists between sessions, which is what makes
-- unplugging and returning later behave like real life.
CREATE TABLE IF NOT EXISTS vehicles (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    battery_capacity_kwh  REAL NOT NULL CHECK (battery_capacity_kwh > 0),
    max_charge_kw         REAL NOT NULL DEFAULT 11.0 CHECK (max_charge_kw > 0),
    current_soc           REAL NOT NULL DEFAULT 20.0 CHECK (current_soc BETWEEN 0 AND 100),
    -- Which real-world DC fast-charging curve shape this car follows, not
    -- just a flat rate. Each one is a distinct, sourced shape -- see
    -- simulator/vehicle.py for the actual curve data and where it came from.
    charge_profile        TEXT NOT NULL DEFAULT 'generic'
                          CHECK (charge_profile IN (
                              'generic', 'renault', 'tesla', 'hyundai_kia',
                              'vw_id', 'nissan_leaf'
                          )),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- ===========================================================================
-- Hardware
-- ===========================================================================

-- `identity` is the string the charger puts in its connection URL path
-- (ws://host/ocpp/<identity>) and is therefore the natural primary key.
CREATE TABLE IF NOT EXISTS charge_points (
    identity              TEXT PRIMARY KEY CHECK (length(identity) BETWEEN 1 AND 64),
    label                 TEXT,

    -- Reported by BootNotification. Not editable from the dashboard: these
    -- describe the hardware, and overwriting them would make the record a
    -- description of nothing.
    vendor                TEXT,
    model                 TEXT,
    serial_number         TEXT,
    firmware_version      TEXT,
    iccid                 TEXT,
    imsi                  TEXT,
    meter_type            TEXT,
    meter_serial_number   TEXT,

    registration_status   TEXT NOT NULL DEFAULT 'Accepted'
                              CHECK (registration_status IN ('Accepted', 'Pending', 'Rejected')),
    heartbeat_interval    INTEGER NOT NULL DEFAULT 300 CHECK (heartbeat_interval > 0),

    -- When 0, Stop ends the transaction instead of holding it at 0 W. Set
    -- automatically if a charger rejects SetChargingProfile.
    supports_charging_profiles INTEGER NOT NULL DEFAULT 1
                              CHECK (supports_charging_profiles IN (0, 1)),

    -- Require a card to have been presented before Start will do anything.
    --
    -- Off by default, because whether it can work at all depends on the
    -- charger: some send Authorize when a card is read, and some handle the
    -- card internally and never tell the CSMS. On the second kind this would
    -- block Start permanently, so it is opt-in per charger rather than a
    -- rule imposed on hardware that cannot participate in it.
    require_card_before_start INTEGER NOT NULL DEFAULT 0
                              CHECK (require_card_before_start IN (0, 1)),

    -- Seconds to wait before answering this charger, for testing how it
    -- behaves when the CSMS is slow. Only replies are held -- never a command
    -- we initiate -- and it is capped well under the 30s OCPP timeout so a
    -- charger does not give up and retry mid-test.
    response_delay_s INTEGER NOT NULL DEFAULT 0
                     CHECK (response_delay_s BETWEEN 0 AND 25),

    -- Last DiagnosticsStatusNotification the charger sent while uploading a
    -- log we requested, so the dashboard can show progress.
    diagnostics_status TEXT,

    is_online             INTEGER NOT NULL DEFAULT 0 CHECK (is_online IN (0, 1)),
    last_seen             TEXT,
    last_boot_at          TEXT,

    -- Set only by the simulator's own provisioning path (POST
    -- /api/charge-points from simulator/main.py), never by a real charger's
    -- BootNotification. This is what lets the simulator tell, on its own
    -- restart, which rows in the CSMS's database are its own fake hardware
    -- to reconnect versus real hardware it must never touch, edit, or claim
    -- as online on the real charger's behalf.
    is_simulated          INTEGER NOT NULL DEFAULT 0 CHECK (is_simulated IN (0, 1)),

    -- A placeholder row that exists only so deleted history (sessions,
    -- transactions, faults) has something valid to point its foreign key
    -- at, after the real charger it belonged to was removed. Never a real
    -- charger -- list queries exclude these.
    is_tombstone          INTEGER NOT NULL DEFAULT 0 CHECK (is_tombstone IN (0, 1)),

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- Connector 0 is reserved by OCPP for the charge point as a whole; physical
-- sockets are numbered from 1.
CREATE TABLE IF NOT EXISTS connectors (
    id                 INTEGER PRIMARY KEY,
    charge_point_id    TEXT    NOT NULL REFERENCES charge_points(identity) ON DELETE CASCADE,
    connector_id       INTEGER NOT NULL CHECK (connector_id >= 0),

    status             TEXT NOT NULL DEFAULT 'Available'
                           CHECK (status IN ('Available', 'Preparing', 'Charging',
                                             'SuspendedEVSE', 'SuspendedEV', 'Finishing',
                                             'Reserved', 'Unavailable', 'Faulted')),
    error_code         TEXT NOT NULL DEFAULT 'NoError'
                           CHECK (error_code IN ('ConnectorLockFailure', 'EVCommunicationError',
                                                 'GroundFailure', 'HighTemperature', 'InternalError',
                                                 'LocalListConflict', 'NoError', 'OtherError',
                                                 'OverCurrentFailure', 'OverVoltage',
                                                 'PowerMeterFailure', 'PowerSwitchFailure',
                                                 'ReaderFailure', 'ResetFailure', 'UnderVoltage',
                                                 'WeakSignal')),
    info               TEXT,
    vendor_error_code  TEXT,
    max_power_kw       REAL,
    status_updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    -- The card presented at this socket, if one has been. Charging cannot be
    -- started until it is set. Survives a pause so resuming needs no second
    -- tap, and is cleared when the cable comes out so the next driver has to
    -- present their own.
    authorized_id_tag  TEXT,

    UNIQUE (charge_point_id, connector_id)
);


-- Mirror of a charge point's GetConfiguration key/value store.
CREATE TABLE IF NOT EXISTS configuration_keys (
    id               INTEGER PRIMARY KEY,
    charge_point_id  TEXT    NOT NULL REFERENCES charge_points(identity) ON DELETE CASCADE,
    key              TEXT    NOT NULL,
    value            TEXT,
    readonly         INTEGER NOT NULL DEFAULT 0 CHECK (readonly IN (0, 1)),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    UNIQUE (charge_point_id, key)
);


-- ===========================================================================
-- Charging
-- ===========================================================================

-- The dashboard-facing unit of work.
--
-- Created when a cable is plugged in and closed when it is removed. Start and
-- Stop toggle between ACTIVE and PAUSED *without* closing the underlying
-- transaction, which is why resuming continues from the exact meter reading
-- rather than from an offset we have to track ourselves.
--
--   WAITING    cable plugged, no transaction open, awaiting Start
--   ACTIVE     transaction open, energy flowing, cable latched
--   PAUSED     transaction open, 0 W profile applied, cable released
--   COMPLETED  cable removed, or the battery reached 100%
--   FAULTED    terminated by a charge point fault
--
-- vehicle_id is bound the moment the cable goes in, so a session always knows
-- which car it belongs to -- it does not wait for a card to be presented.
CREATE TABLE IF NOT EXISTS charging_sessions (
    id               INTEGER PRIMARY KEY,
    charge_point_id  TEXT    NOT NULL REFERENCES charge_points(identity) ON DELETE CASCADE,
    connector_pk     INTEGER NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    connector_id     INTEGER NOT NULL,

    id_tag           TEXT REFERENCES id_tags(id_tag) ON DELETE SET NULL,
    vehicle_id       INTEGER REFERENCES vehicles(id) ON DELETE SET NULL,

    state            TEXT NOT NULL DEFAULT 'WAITING'
                         CHECK (state IN ('WAITING', 'ACTIVE', 'PAUSED',
                                          'COMPLETED', 'FAULTED')),

    plugged_in_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at       TEXT,
    ended_at         TEXT,

    energy_wh        INTEGER NOT NULL DEFAULT 0 CHECK (energy_wh >= 0),
    -- Seconds spent in ACTIVE, excluding held time. Drained from active_since
    -- on every exit from ACTIVE, so it is a measurement and not an estimate.
    active_seconds   INTEGER NOT NULL DEFAULT 0 CHECK (active_seconds >= 0),
    active_since     TEXT,

    end_reason       TEXT CHECK (end_reason IS NULL OR end_reason IN (
                         'EmergencyStop', 'EVDisconnected', 'HardReset', 'Local', 'Other',
                         'PowerLoss', 'Reboot', 'Remote', 'SoftReset', 'UnlockCommand',
                         'DeAuthorized')),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_sessions_cp_state    ON charging_sessions (charge_point_id, state);
CREATE INDEX IF NOT EXISTS ix_sessions_connector   ON charging_sessions (connector_pk, state);
CREATE INDEX IF NOT EXISTS ix_sessions_started_at  ON charging_sessions (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_vehicle     ON charging_sessions (vehicle_id, started_at DESC);

-- At most one open session per connector, enforced by the storage engine
-- rather than by application locking.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_one_open_per_connector
    ON charging_sessions (connector_pk)
    WHERE state IN ('WAITING', 'ACTIVE', 'PAUSED', 'FAULTED');

-- A car can only be plugged into one socket at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_one_open_per_vehicle
    ON charging_sessions (vehicle_id)
    WHERE vehicle_id IS NOT NULL AND state IN ('WAITING', 'ACTIVE', 'PAUSED', 'FAULTED');


-- The OCPP-level transaction record. `ocpp_transaction_id` is what appears on
-- the wire; `id` is our own surrogate key so the two stay separable.
CREATE TABLE IF NOT EXISTS transactions (
    id                   INTEGER PRIMARY KEY,
    ocpp_transaction_id  INTEGER NOT NULL UNIQUE,
    session_id           INTEGER NOT NULL REFERENCES charging_sessions(id) ON DELETE CASCADE,
    charge_point_id      TEXT    NOT NULL REFERENCES charge_points(identity) ON DELETE CASCADE,
    connector_id         INTEGER NOT NULL,
    id_tag               TEXT,

    state                TEXT NOT NULL DEFAULT 'ACTIVE'
                             CHECK (state IN ('ACTIVE', 'STOPPED')),

    meter_start_wh       INTEGER NOT NULL CHECK (meter_start_wh >= 0),
    meter_stop_wh        INTEGER CHECK (meter_stop_wh IS NULL OR meter_stop_wh >= meter_start_wh),
    meter_last_wh        INTEGER CHECK (meter_last_wh IS NULL OR meter_last_wh >= meter_start_wh),

    started_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    stopped_at           TEXT,
    stop_reason          TEXT CHECK (stop_reason IS NULL OR stop_reason IN (
                             'EmergencyStop', 'EVDisconnected', 'HardReset', 'Local', 'Other',
                             'PowerLoss', 'Reboot', 'Remote', 'SoftReset', 'UnlockCommand',
                             'DeAuthorized')),
    reservation_id       INTEGER,

    CHECK ((state = 'STOPPED') = (stopped_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_transactions_session  ON transactions (session_id);
CREATE INDEX IF NOT EXISTS ix_transactions_cp_state ON transactions (charge_point_id, state);


-- One sampled value from a MeterValues message.
CREATE TABLE IF NOT EXISTS meter_values (
    id               INTEGER PRIMARY KEY,
    transaction_id   INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
    session_id       INTEGER REFERENCES charging_sessions(id) ON DELETE CASCADE,
    charge_point_id  TEXT    NOT NULL,
    connector_id     INTEGER NOT NULL,

    timestamp        TEXT NOT NULL,
    measurand        TEXT NOT NULL
                         CHECK (measurand IN ('Energy.Active.Import.Register',
                                              'Power.Active.Import', 'Current.Import',
                                              'Current.Offered', 'Voltage', 'SoC',
                                              'Temperature')),
    value            REAL NOT NULL,
    unit             TEXT,
    phase            TEXT,
    context          TEXT CHECK (context IS NULL OR context IN (
                         'Interruption.Begin', 'Interruption.End', 'Other', 'Sample.Clock',
                         'Sample.Periodic', 'Transaction.Begin', 'Transaction.End', 'Trigger'))
);

CREATE INDEX IF NOT EXISTS ix_meter_values_txn_ts
    ON meter_values (transaction_id, timestamp);
CREATE INDEX IF NOT EXISTS ix_meter_values_session_measurand
    ON meter_values (session_id, measurand, timestamp);


-- Bookkeeping for profiles we pushed. Pausing installs a TxProfile limited to
-- 0 W; resuming must clear exactly that profile and nothing else.
CREATE TABLE IF NOT EXISTS charging_profiles (
    id               INTEGER PRIMARY KEY,
    charge_point_id  TEXT    NOT NULL REFERENCES charge_points(identity) ON DELETE CASCADE,
    connector_id     INTEGER NOT NULL,
    session_id       INTEGER REFERENCES charging_sessions(id) ON DELETE CASCADE,

    ocpp_profile_id  INTEGER NOT NULL,
    purpose          TEXT NOT NULL DEFAULT 'TxProfile'
                         CHECK (purpose IN ('ChargePointMaxProfile', 'TxDefaultProfile',
                                            'TxProfile')),
    stack_level      INTEGER NOT NULL DEFAULT 0 CHECK (stack_level >= 0),
    limit_value      REAL    NOT NULL CHECK (limit_value >= 0),
    limit_unit       TEXT    NOT NULL DEFAULT 'W' CHECK (limit_unit IN ('W', 'A')),

    applied_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    cleared_at       TEXT
);

CREATE INDEX IF NOT EXISTS ix_profiles_active
    ON charging_profiles (charge_point_id, connector_id, cleared_at);


-- ===========================================================================
-- Observability
-- ===========================================================================

-- Every OCPP frame in both directions.
--
-- `action` is stored on results too. A CALLRESULT does not carry an action on
-- the wire -- it is matched to its request by message ID -- so the connection
-- remembers the action while a call is outstanding and stamps it here. Without
-- that, half the log reads "result 3" and tells you nothing.
-- One row per Faulted occurrence on a connector. Written when a Faulted
-- StatusNotification arrives and closed off (cleared_at) on the next status
-- that isn't Faulted. Independent of session lifecycle: a fault no longer
-- closes the session or its transaction, since the charger itself often keeps
-- both running straight through the fault window. This table is purely the
-- historical record of "a fault happened here, this is what it was."
CREATE TABLE IF NOT EXISTS faults (
    id                  INTEGER PRIMARY KEY,
    charge_point_id     TEXT    NOT NULL REFERENCES charge_points(identity) ON DELETE CASCADE,
    connector_id        INTEGER NOT NULL,
    session_id          INTEGER REFERENCES charging_sessions(id) ON DELETE SET NULL,

    error_code          TEXT,
    vendor_error_code   TEXT,
    info                TEXT,

    occurred_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    cleared_at          TEXT
);

CREATE INDEX IF NOT EXISTS ix_faults_cp_ts      ON faults (charge_point_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_faults_session    ON faults (session_id);

-- At most one open fault per connector at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_faults_one_open_per_connector
    ON faults (charge_point_id, connector_id)
    WHERE cleared_at IS NULL;


-- One row per WebSocket connect/disconnect, purely to reconstruct an uptime
-- history: the current streak, a 24h/7d timeline, and reliability
-- percentages. Collection starts the moment this ships -- there is no
-- retroactive history, so a freshly-added charger's timeline is empty until
-- it has actually been running for a while.
CREATE TABLE IF NOT EXISTS connection_events (
    id                  INTEGER PRIMARY KEY,
    charge_point_id     TEXT    NOT NULL REFERENCES charge_points(identity) ON DELETE CASCADE,
    event               TEXT    NOT NULL CHECK (event IN ('connected', 'disconnected')),
    occurred_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_connection_events_cp_ts
    ON connection_events (charge_point_id, occurred_at DESC);


CREATE TABLE IF NOT EXISTS message_log (
    id                 INTEGER PRIMARY KEY,
    charge_point_id    TEXT,
    direction          TEXT NOT NULL CHECK (direction IN ('INBOUND', 'OUTBOUND')),
    message_type_id    INTEGER NOT NULL CHECK (message_type_id IN (2, 3, 4)),
    -- OCPP 1.6 caps the unique message ID at 36 characters, to allow for GUIDs.
    unique_id          TEXT CHECK (unique_id IS NULL OR length(unique_id) <= 36),
    action             TEXT,
    payload            TEXT,        -- JSON

    error_code         TEXT,
    error_description  TEXT,
    error_details      TEXT,        -- JSON

    session_id         INTEGER,
    timestamp          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_message_log_cp_ts     ON message_log (charge_point_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_message_log_action    ON message_log (action);
CREATE INDEX IF NOT EXISTS ix_message_log_unique_id ON message_log (unique_id);


-- ===========================================================================
-- Views
-- ===========================================================================

DROP VIEW IF EXISTS v_active_sessions;
CREATE VIEW v_active_sessions AS
SELECT
    s.id                AS session_id,
    s.charge_point_id,
    s.connector_id,
    s.state,
    s.id_tag,
    s.plugged_in_at,
    s.started_at,
    s.energy_wh,
    s.active_seconds,
    s.active_seconds + CASE
        WHEN s.active_since IS NULL THEN 0
        ELSE CAST((julianday('now') - julianday(replace(s.active_since, 'Z', ''))) * 86400 AS INTEGER)
    END                 AS active_seconds_live,
    CASE WHEN s.state = 'ACTIVE' THEN 1 ELSE 0 END AS cable_locked,
    cp.label            AS charge_point_label,
    c.status            AS connector_status,
    c.error_code        AS connector_error_code,
    c.max_power_kw,
    v.id                AS vehicle_id,
    v.name              AS vehicle_name,
    v.current_soc,
    v.battery_capacity_kwh,
    t.ocpp_transaction_id,
    t.meter_start_wh,
    t.meter_last_wh
FROM charging_sessions s
JOIN charge_points cp ON cp.identity = s.charge_point_id
JOIN connectors    c  ON c.id = s.connector_pk
LEFT JOIN vehicles v  ON v.id = s.vehicle_id
LEFT JOIN transactions t
       ON t.session_id = s.id AND t.state = 'ACTIVE'
WHERE s.state IN ('WAITING', 'ACTIVE', 'PAUSED');


DROP VIEW IF EXISTS v_connector_overview;
CREATE VIEW v_connector_overview AS
SELECT
    c.id               AS connector_pk,
    c.charge_point_id,
    c.connector_id,
    c.status,
    c.error_code,
    c.max_power_kw,
    c.status_updated_at,
    cp.label           AS charge_point_label,
    cp.is_online,
    cp.last_seen,
    s.id               AS session_id,
    s.state            AS session_state,
    s.energy_wh        AS session_energy_wh,
    s.started_at       AS session_started_at,
    s.active_seconds + CASE
        WHEN s.active_since IS NULL THEN 0
        ELSE CAST((julianday('now') - julianday(replace(s.active_since, 'Z', ''))) * 86400 AS INTEGER)
    END                AS session_active_seconds,
    CASE WHEN s.state = 'ACTIVE' THEN 1 ELSE 0 END AS cable_locked,
    -- The limit in force right now, if any, so the slider shows its real
    -- position rather than a guess that drifts from the charger.
    (SELECT p.limit_value FROM charging_profiles p
      WHERE p.charge_point_id = c.charge_point_id
        AND p.connector_id = c.connector_id
        AND p.cleared_at IS NULL
      ORDER BY p.id DESC LIMIT 1)          AS active_limit,
    (SELECT p.limit_unit FROM charging_profiles p
      WHERE p.charge_point_id = c.charge_point_id
        AND p.connector_id = c.connector_id
        AND p.cleared_at IS NULL
      ORDER BY p.id DESC LIMIT 1)          AS active_limit_unit,
    c.authorized_id_tag,
    s.id_tag           AS session_id_tag,
    v.id               AS vehicle_id,
    v.name             AS vehicle_name,
    v.current_soc,
    v.battery_capacity_kwh
FROM connectors c
JOIN charge_points cp ON cp.identity = c.charge_point_id
LEFT JOIN charging_sessions s
       ON s.connector_pk = c.id
      AND s.state IN ('WAITING', 'ACTIVE', 'PAUSED')
LEFT JOIN vehicles v ON v.id = s.vehicle_id
WHERE c.connector_id > 0;

-- A saved, reusable stress-test template: a name and its ordered step list.
-- Deliberately separate from an actual run -- a run is a real, in-progress
-- or completed execution (tracked in memory on the CSMS process, not
-- persisted, since it is genuinely transient); this table is the reusable
-- definition you come back to and re-run whenever you want, which is what
-- needs to survive a restart and be shared across devices.
CREATE TABLE IF NOT EXISTS test_definitions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    -- The step list, exactly as the stress-test runner already understands
    -- it (see csms/domain/stress_test.py's Step dataclass) -- stored as
    -- JSON since it is a small, self-contained, ordered list with no
    -- relational structure worth normalising into its own tables.
    steps_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);