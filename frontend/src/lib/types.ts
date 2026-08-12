/** Shapes returned by the CSMS API. Kept in one file so a backend change
 *  surfaces as a type error rather than a runtime surprise. */

export type SessionState =
  | "WAITING"
  | "ACTIVE"
  | "PAUSED"
  | "COMPLETED"
  | "FAULTED";

export type ConnectorStatus =
  | "Available"
  | "Preparing"
  | "Charging"
  | "SuspendedEVSE"
  | "SuspendedEV"
  | "Finishing"
  | "Reserved"
  | "Unavailable"
  | "Faulted";

export interface Connector {
  id: number;
  charge_point_id: string;
  connector_id: number;
  status: ConnectorStatus;
  error_code: string;
  info: string | null;
  max_power_kw: number | null;
  status_updated_at: string;
}

export interface ChargePoint {
  identity: string;
  label: string | null;
  vendor: string | null;
  model: string | null;
  serial_number: string | null;
  firmware_version: string | null;
  registration_status: string;
  heartbeat_interval: number;
  supports_charging_profiles: number;
  /** Opt-in: only chargers that report card reads can be gated on one. */
  require_card_before_start: number;
  response_delay_s: number;
  is_online: number;
  is_simulated: number;
  last_seen: string | null;
  live?: boolean;
  connectors?: Connector[];
  configuration?: { key: string; value: string | null; readonly: number }[];
}

/** How long a charger has been in its current connected/disconnected state.
 *  Both since and seconds are null when there is no history at all yet. */
export interface UptimeStreak {
  since: string | null;
  seconds: number | null;
  connected: boolean;
}

export interface UptimeSummary {
  streak: UptimeStreak;
  percent_48h: number | null;
  percent_7d: number | null;
}

/** One stretch of the timeline. connected is null for a period before any
 *  history existed -- genuinely unknown, not assumed either way. */
export interface UptimeSegment {
  start: string;
  end: string;
  connected: boolean | null;
}

export interface UptimeTimeline {
  window: "24h" | "48h" | "7d";
  segments: UptimeSegment[];
  percent: number | null;
}

export interface ConnectorOverview {
  connector_pk: number;
  charge_point_id: string;
  connector_id: number;
  status: ConnectorStatus;
  error_code: string;
  max_power_kw: number | null;
  status_updated_at: string;
  charge_point_label: string | null;
  is_online: number;
  last_seen: string | null;
  session_id: number | null;
  session_state: SessionState | null;
  session_energy_wh: number | null;
  session_started_at: string | null;
  session_active_seconds: number | null;
  /** The connector latch: engaged only while power is flowing. */
  cable_locked: number;
  /** The card presented at this socket. Start is disabled until it is set. */
  authorized_id_tag: string | null;
  /** The charging limit in force, if we have installed one. */
  active_limit: number | null;
  active_limit_unit: string | null;
  session_id_tag: string | null;
  vehicle_id: number | null;
  vehicle_name: string | null;
  current_soc: number | null;
  battery_capacity_kwh: number | null;
}

export interface Transaction {
  id: number;
  ocpp_transaction_id: number;
  state: "ACTIVE" | "STOPPED";
  meter_start_wh: number;
  meter_stop_wh: number | null;
  meter_last_wh: number | null;
  started_at: string;
  stopped_at: string | null;
  stop_reason: string | null;
  id_tag: string | null;
}

export interface SeriesPoint {
  t: string;
  v: number;
  unit: string | null;
}

export interface LogRow {
  id: number;
  charge_point_id: string | null;
  direction: "INBOUND" | "OUTBOUND";
  message_type_id: number;
  unique_id: string | null;
  action: string | null;
  payload: string | null;
  error_code: string | null;
  error_description: string | null;
  error_details: string | null;
  timestamp: string;
}

export interface Fault {
  id: number;
  charge_point_id: string;
  charge_point_label: string | null;
  connector_id: number;
  session_id: number | null;
  error_code: string | null;
  vendor_error_code: string | null;
  info: string | null;
  occurred_at: string;
  cleared_at: string | null;
}

export interface Session {
  id: number;
  charge_point_id: string;
  connector_id: number;
  id_tag: string | null;
  vehicle_id: number | null;
  state: SessionState;
  plugged_in_at: string;
  started_at: string | null;
  ended_at: string | null;
  energy_wh: number;
  active_seconds: number;
  /** Ticks while ACTIVE; equals active_seconds once the session closes. */
  active_seconds_live?: number;
  cable_locked?: number;
  end_reason: string | null;
  vehicle_name?: string | null;
  battery_capacity_kwh?: number | null;
  charge_point_label?: string | null;
  transactions?: Transaction[];
  series?: Record<string, SeriesPoint[]>;
  messages?: LogRow[];
}

export interface Overview {
  connectors: ConnectorOverview[];
  sessions: Record<string, unknown>[];
  energy_by_day: { day: string; kwh: number; sessions: number }[];
  /** Hour buckets ("2026-07-22T14"), from differenced meter readings. */
  energy_by_hour: { hour: string; kwh: number }[];
  connected: string[];
}

/** A card is a credential and nothing more. */
export interface IdTag {
  id_tag: string;
  status: "Accepted" | "Blocked" | "Expired" | "Invalid" | "ConcurrentTx";
  expiry_date: string | null;
  created_at: string;
  session_id: number | null;
  charge_point_id: string | null;
  connector_id: number | null;
}

export type ChargeProfile =
  | "generic"
  | "renault"
  | "tesla"
  | "hyundai_kia"
  | "vw_id"
  | "nissan_leaf";

export interface Vehicle {
  id: number;
  name: string;
  battery_capacity_kwh: number;
  max_charge_kw: number;
  current_soc: number;
  /** Which real-world DC fast-charging curve shape this car follows. */
  charge_profile: ChargeProfile;
  /** Where this car is plugged in, if it is plugged in anywhere. */
  session_id: number | null;
  charge_point_id: string | null;
  connector_id: number | null;
  session_state: SessionState | null;
}

export interface CommandResult {
  ok: boolean;
  session_id: number | null;
  state: string | null;
  detail: string;
}

/** Live feed payload from /ws/dashboard. */
export interface LiveEvent {
  topic: string;
  timestamp: string;
  [key: string]: unknown;
}

/** Simulator control API. */
export interface SimConnector {
  connector_id: number;
  status: string;
  meter_wh: number;
  transaction_id: number | null;
  power_limit_w: number | null;
  paused: boolean;
  cable_locked: boolean;
  power_offered: boolean;
  active_id_tag: string | null;
  vehicle: { id: number; name: string; soc: number; capacity_kwh: number } | null;
}

/** One simulated charger. connected is false while it is still negotiating
 *  its WebSocket connection, or reconnecting after a drop -- it still shows
 *  up in the list rather than vanishing, the same way an offline real
 *  charger would still appear on the dashboard. held_offline is true only
 *  when an operator deliberately asked it to stay down; a drop that has not
 *  been asked for yet shows connected: false with held_offline: false, and
 *  the retry loop is already working on reconnecting it. */
export interface SimCharger {
  identity: string;
  connected: boolean;
  held_offline: boolean;
  time_scale: number | null;
  meter_sample_interval: number | null;
  full_dwell_seconds: number | null;
  handshake_seconds: number | null;
  max_power_kw: number | null;
  connectors: SimConnector[];
}

export interface SimChargers {
  chargers: SimCharger[];
}

/** A diagnostics archive the charger uploaded after GetDiagnostics. */
export interface DiagnosticsFile {
  name: string;
  bytes: number;
  received_at: string;
}