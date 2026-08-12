/** API client and React Query hooks.
 *
 *  One thin fetch wrapper, then a hook per resource. Mutations invalidate the
 *  queries they affect, and the live WebSocket feed invalidates on top of that
 *  (see useLiveFeed), so the UI stays current without aggressive polling.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import type {
  DiagnosticsFile,
  ChargePoint,
  CommandResult,
  IdTag,
  LogRow,
  Fault,
  UptimeSummary,
  UptimeTimeline,
  Overview,
  Session,
  SimChargers,
  Vehicle,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    // The API returns {detail: "..."} for 409 and 502, which is the message
    // worth showing the operator verbatim.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* response had no JSON body */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : "{}" });

const patch = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });

const del = <T,>(path: string) => request<T>(path, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useOverview(): UseQueryResult<Overview> {
  return useQuery({
    queryKey: ["overview"],
    queryFn: () => request<Overview>("/api/overview"),
    refetchInterval: 5000,
  });
}

export function useChargePoints(): UseQueryResult<ChargePoint[]> {
  return useQuery({
    queryKey: ["charge-points"],
    queryFn: () => request<ChargePoint[]>("/api/charge-points"),
  });
}

export function useChargePoint(identity: string): UseQueryResult<ChargePoint> {
  return useQuery({
    queryKey: ["charge-point", identity],
    queryFn: () => request<ChargePoint>(`/api/charge-points/${identity}`),
    enabled: Boolean(identity),
  });
}

/** The current streak plus both 48h and 7d percentages -- always both,
 *  regardless of which panel is being looked at. */
export function useUptimeSummary(identity: string): UseQueryResult<UptimeSummary> {
  return useQuery({
    queryKey: ["uptime-summary", identity],
    queryFn: () => request<UptimeSummary>(`/api/charge-points/${identity}/uptime`),
    enabled: Boolean(identity),
    refetchInterval: 30_000,
  });
}

export function useUptimeTimeline(
  identity: string,
  window: "24h" | "48h" | "7d",
): UseQueryResult<UptimeTimeline> {
  return useQuery({
    queryKey: ["uptime-timeline", identity, window],
    queryFn: () =>
      request<UptimeTimeline>(
        `/api/charge-points/${identity}/uptime/timeline?window=${window}`,
      ),
    enabled: Boolean(identity),
    refetchInterval: 30_000,
  });
}

export function useSessions(params: {
  charge_point_id?: string;
  vehicle_id?: number;
  limit?: number;
} = {}): UseQueryResult<Session[]> {
  const search = new URLSearchParams();
  if (params.charge_point_id) search.set("charge_point_id", params.charge_point_id);
  if (params.vehicle_id) search.set("vehicle_id", String(params.vehicle_id));
  search.set("limit", String(params.limit ?? 100));
  return useQuery({
    queryKey: ["sessions", params],
    queryFn: () => request<Session[]>(`/api/sessions?${search}`),
  });
}

export function useSession(id: number | null): UseQueryResult<Session> {
  return useQuery({
    queryKey: ["session", id],
    queryFn: () => request<Session>(`/api/sessions/${id}`),
    enabled: id != null,
    refetchInterval: 4000,
  });
}

export function useTags(): UseQueryResult<IdTag[]> {
  return useQuery({
    queryKey: ["tags"],
    queryFn: () => request<IdTag[]>("/api/tags"),
    refetchInterval: 8000,
  });
}

export function useVehicles(): UseQueryResult<Vehicle[]> {
  return useQuery({
    queryKey: ["vehicles"],
    queryFn: () => request<Vehicle[]>("/api/vehicles"),
    refetchInterval: 8000,
  });
}

export function useLogs(filters: {
  charge_point_id?: string;
  action?: string;
  direction?: string;
  limit?: number;
}): UseQueryResult<LogRow[]> {
  const search = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => {
    if (v) search.set(k, String(v));
  });
  if (!search.has("limit")) search.set("limit", "200");
  return useQuery({
    queryKey: ["logs", filters],
    queryFn: () => request<LogRow[]>(`/api/logs?${search}`),
    refetchInterval: 4000,
  });
}

export function useFaults(filters: {
  charge_point_id?: string;
  session_id?: number;
  limit?: number;
}): UseQueryResult<Fault[]> {
  const search = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => {
    if (v !== undefined && v !== "") search.set(k, String(v));
  });
  if (!search.has("limit")) search.set("limit", "200");
  return useQuery({
    queryKey: ["faults", filters],
    queryFn: () => request<Fault[]>(`/api/faults?${search}`),
    refetchInterval: 4000,
  });
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/** Everything a session command touches. Invalidated together so a state
 *  change lands everywhere at once rather than page by page. */
function invalidateSessionViews(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["overview"] });
  qc.invalidateQueries({ queryKey: ["sessions"] });
  qc.invalidateQueries({ queryKey: ["session"] });
  qc.invalidateQueries({ queryKey: ["charge-point"] });
}

export function useStartCharging() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { identity: string; connector_id: number; id_tag?: string }) =>
      post<CommandResult>(`/api/charge-points/${vars.identity}/start`, {
        connector_id: vars.connector_id,
        id_tag: vars.id_tag ?? null,
      }),
    onSuccess: () => invalidateSessionViews(qc),
  });
}

/** Hold at zero power. The transaction stays open and the latch releases, so
 *  unplugging is what actually ends the session. */
export function useStopCharging() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: number) =>
      post<CommandResult>(`/api/sessions/${sessionId}/stop`),
    onSuccess: () => invalidateSessionViews(qc),
  });
}

/** Recovery only: clears a session whose charger died and will never report
 *  again. Everything else goes through the charger. */
// ---------------------------------------------------------------------------
// Directory writes
//
// One factory rather than twelve near-identical hooks. Each returns a mutation
// that refetches the collections its change could affect -- a vehicle edit can
// alter what a tag row shows, so both are invalidated.
// ---------------------------------------------------------------------------

type Collection = "tags" | "vehicles" | "charge-points";

const AFFECTED: Record<Collection, string[]> = {
  tags: ["tags", "vehicles"],
  vehicles: ["vehicles", "tags", "overview", "sim-state"],
  "charge-points": ["charge-points", "charge-point", "overview"],
};

function useDirectoryMutation<TVars>(
  collection: Collection,
  run: (vars: TVars) => Promise<unknown>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: () =>
      AFFECTED[collection].forEach((key) =>
        qc.invalidateQueries({ queryKey: [key] }),
      ),
  });
}

/** Push a setting to a live charger with ChangeConfiguration.
 *
 *  This changes the charger itself, not our record of it -- our mirror is
 *  updated only once the charger accepts. A charger may refuse a key outright
 *  or report it read-only, and that answer is worth showing verbatim.
 */
export function useChangeConfiguration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { identity: string; key: string; value: string }) =>
      post<{ status: string }>(
        `/api/charge-points/${vars.identity}/configuration`,
        { key: vars.key, value: vars.value },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["charge-point"] });
      qc.invalidateQueries({ queryKey: ["charge-points"] });
    },
  });
}

/** The OCPP commands, each named for the action it sends.
 *
 *  Every one returns the charger's answer untouched. A rejection is the
 *  useful part -- it is how you learn what a particular unit supports.
 */
/** RemoteStopTransaction: end the transaction rather than hold it.
 *
 *  Stop keeps the transaction open so it can be resumed. This closes it.
 */
/** Cap how much the charger may deliver, without stopping it. */
export function useLimitSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { sessionId: number; limit: number }) =>
      post<CommandResult>(`/api/sessions/${vars.sessionId}/limit`, {
        limit: vars.limit,
      }),
    onSuccess: () => invalidateSessionViews(qc),
  });
}

export function useEndSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: number) =>
      post<CommandResult>(`/api/sessions/${sessionId}/end`),
    onSuccess: () => invalidateSessionViews(qc),
  });
}

/** Diagnostics files the charger has uploaded to us. */
export function useDiagnosticsFiles(chargePointId?: string) {
  return useQuery({
    queryKey: ["diagnostics", chargePointId ?? "all"],
    queryFn: () =>
      request<DiagnosticsFile[]>(
        chargePointId
          ? `/api/diagnostics?charge_point_id=${encodeURIComponent(chargePointId)}`
          : "/api/diagnostics",
      ),
    refetchInterval: 5_000,
  });
}

export function useChargerCommand(
  command:
    | "get-configuration"
    | "diagnostics"
    | "trigger"
    | "reset"
    | "clear-cache"
    | "unlock"
    | "availability"
    | "get-local-list-version"
    | "send-local-list"
    | "reserve-now"
    | "cancel-reservation"
    | "composite-schedule"
    | "update-firmware"
    | "data-transfer",
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { identity: string; body?: Record<string, unknown> }) =>
      post<Record<string, unknown>>(
        `/api/charge-points/${vars.identity}/${command}`,
        vars.body ?? {},
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["charge-point"] });
      qc.invalidateQueries({ queryKey: ["overview"] });
    },
  });
}

export const useCreateTag = () =>
  useDirectoryMutation<Record<string, unknown>>("tags", (body) =>
    post("/api/tags", body),
  );

export const useUpdateTag = () =>
  useDirectoryMutation<{ id_tag: string; changes: Record<string, unknown> }>(
    "tags",
    ({ id_tag, changes }) => patch(`/api/tags/${id_tag}`, changes),
  );

export const useDeleteTag = () =>
  useDirectoryMutation<string>("tags", (id_tag) => del(`/api/tags/${id_tag}`));

export const useCreateVehicle = () =>
  useDirectoryMutation<Record<string, unknown>>("vehicles", (body) =>
    post("/api/vehicles", body),
  );

export const useUpdateVehicle = () =>
  useDirectoryMutation<{ id: number; changes: Record<string, unknown> }>(
    "vehicles",
    ({ id, changes }) => patch(`/api/vehicles/${id}`, changes),
  );

export const useDeleteVehicle = () =>
  useDirectoryMutation<number>("vehicles", (id) => del(`/api/vehicles/${id}`));

export const useUpdateChargePoint = () =>
  useDirectoryMutation<{ identity: string; changes: Record<string, unknown> }>(
    "charge-points",
    ({ identity, changes }) => patch(`/api/charge-points/${identity}`, changes),
  );

/** Provisions a charger before it has ever connected -- the same real-hardware
 *  identity, connector count, and per-connector max kW a physical unit would
 *  need pre-registered. The simulator's "add charger" calls this exact
 *  endpoint too, so a simulated charger is provisioned identically to a real
 *  one rather than living in a second, disconnected notion of what exists. */
export const useCreateChargePoint = () =>
  useDirectoryMutation<Record<string, unknown>>("charge-points", (body) =>
    post("/api/charge-points", body),
  );

/** Removes a charger and everything cascaded from it. Refused server-side
 *  while a session is genuinely open. */
export const useDeleteChargePoint = () =>
  useDirectoryMutation<string>("charge-points", (identity) =>
    del(`/api/charge-points/${identity}`),
  );

// ---------------------------------------------------------------------------
// Simulator control (proxied to :9100 by vite)
// ---------------------------------------------------------------------------

export function useSimChargers(): UseQueryResult<SimChargers> {
  return useQuery({
    queryKey: ["sim-chargers"],
    queryFn: () => request<SimChargers>("/sim/chargers"),
    refetchInterval: 2000,
    retry: false,
  });
}

export interface SimResult {
  ok: boolean;
  /** Present on /swipe: the verbatim idTagInfo status from the CSMS. */
  status?: string;
}

export function useSimCommand(path: "plug" | "unplug" | "swipe" | "fault" | "power") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      post<SimResult>(`/sim/${path}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sim-chargers"] });
      qc.invalidateQueries({ queryKey: ["vehicles"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
      invalidateSessionViews(qc);
    },
  });
}

export interface AddChargerResult {
  ok: boolean;
  identity: string;
  connectors: number;
}

export interface AddChargerBody {
  identity: string;
  connectors: number;
  label?: string;
  max_power_kw?: number;
  heartbeat_interval?: number;
  registration_status?: string;
  supports_charging_profiles?: boolean;
  require_card_before_start?: boolean;
  vendor?: string;
  model?: string;
  serial_number?: string;
  firmware_version?: string;
  time_scale?: number;
  meter_sample_interval?: number;
  full_dwell_seconds?: number;
}

/** Spins up a new independent simulated charger -- its own WebSocket
 *  connection to the CSMS, entirely separate from any other running one. */
export function useAddSimCharger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AddChargerBody) => post<AddChargerResult>("/sim/chargers", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sim-chargers"] });
    },
  });
}

export function useRemoveSimCharger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (identity: string) => del<{ ok: boolean }>(`/sim/chargers/${identity}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sim-chargers"] });
    },
  });
}

/** A genuine WebSocket disconnect/reconnect, the same as a cable being
 *  pulled -- not a polite goodbye the charger negotiates. */
export function useSimConnectionToggle(action: "online" | "offline") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (identity: string) =>
      post<{ ok: boolean }>(`/sim/chargers/${identity}/${action}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sim-chargers"] });
    },
  });
}

export interface SimSettingsPatch {
  identity: string;
  time_scale?: number;
  meter_sample_interval?: number;
  max_power_kw?: number;
  full_dwell_seconds?: number;
}

/** Every OCPP command a charger can genuinely refuse outright, and whether
 *  each is currently blocked for one specific charger. */
export const BLOCKABLE_ACTIONS = [
  "RemoteStartTransaction",
  "RemoteStopTransaction",
  "SetChargingProfile",
  "ClearChargingProfile",
  "ChangeConfiguration",
  "Reset",
  "UnlockConnector",
  "TriggerMessage",
] as const;

export type BlockableAction = (typeof BLOCKABLE_ACTIONS)[number];

export function useBlockedActions(identity: string) {
  return useQuery({
    queryKey: ["sim-blocked-actions", identity],
    queryFn: () =>
      request<{ identity: string; actions: Record<BlockableAction, boolean> }>(
        `/sim/block-action?identity=${encodeURIComponent(identity)}`,
      ),
    refetchInterval: 5000,
  });
}

export function useToggleBlockedAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { identity: string; action: BlockableAction; blocked: boolean }) =>
      post<{ ok: boolean }>("/sim/block-action", body),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["sim-blocked-actions", vars.identity] });
    },
  });
}

/** Every field here is a plain mutable attribute the simulator's own loops
 *  read fresh each tick, so this takes effect immediately -- no reconnect. */
export function useUpdateSimSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ identity, ...changes }: SimSettingsPatch) =>
      patch<{ ok: boolean }>(`/sim/chargers/${identity}/settings`, {
        identity,
        ...changes,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sim-chargers"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Stress testing
//
// A run is server-orchestrated: the browser sends the whole step sequence
// once and polls for progress, rather than looping through 1000 creates
// itself. The CSMS is what actually calls the simulator's own control API
// to create real chargers, so this hits /api/stress-tests on the CSMS, not
// /sim/... on the simulator directly.
// ---------------------------------------------------------------------------

export type StressStepKind =
  | "create"
  | "plug_in"
  | "unplug"
  | "present_card"
  | "charge"
  | "stop_charge"
  | "remote_start"
  | "remote_stop"
  | "fault"
  | "clear_fault"
  | "wait"
  | "delete";

export interface StressStep {
  kind: StressStepKind;
  count?: number;
  connectors?: number;
  seconds?: number;
  delete_target?: "all" | "created_here";
}

export interface StressStepResult {
  kind: StressStepKind;
  ok: boolean;
  detail: string;
  started_at: number;
  finished_at: number | null;
}

export interface StressRun {
  id: string;
  name: string;
  status: "running" | "done" | "failed" | "cancelled";
  created_count: number;
  steps: StressStepResult[];
  total_steps: number;
}

export function useStartStressTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; steps: StressStep[] }) =>
      post<StressRun>("/api/stress-tests", body),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["stress-tests"] });
      qc.setQueryData(["stress-test", run.id], run);
    },
  });
}

/** Polls while a run is genuinely still going; stops once it settles into a
 *  final status, so a finished run does not keep hitting the network. */
/** Every run this CSMS process knows about, most recent first. This is
 *  what actually survives navigating away and back -- it re-reads the
 *  real, server-side truth fresh on every mount, rather than a local
 *  useState that React destroys the moment the page unmounts. */
export function useStressTestList() {
  return useQuery({
    queryKey: ["stress-tests"],
    queryFn: () => request<StressRun[]>("/api/stress-tests"),
    refetchInterval: (query) => {
      const data = query.state.data as StressRun[] | undefined;
      return data?.some((r) => r.status === "running") ? 1000 : false;
    },
  });
}

export function useStressTest(runId: string | null) {
  return useQuery({
    queryKey: ["stress-test", runId],
    queryFn: () => request<StressRun>(`/api/stress-tests/${runId}`),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const data = query.state.data as StressRun | undefined;
      return data && data.status === "running" ? 750 : false;
    },
  });
}

export function useCancelStressTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => post<{ ok: boolean }>(`/api/stress-tests/${runId}/cancel`),
    onSuccess: (_data, runId) => {
      qc.invalidateQueries({ queryKey: ["stress-test", runId] });
    },
  });
}

// ---------------------------------------------------------------------------
// Saved test definitions
//
// A definition is a reusable, named template, persisted on the backend --
// separate from an actual run (StressRun above), which stays transient.
// This is what makes "build a test once, run it whenever" possible.
// ---------------------------------------------------------------------------

export interface TestDefinition {
  id: number;
  name: string;
  steps: StressStep[];
  created_at: string;
}

export function useTestDefinitions() {
  return useQuery({
    queryKey: ["test-definitions"],
    queryFn: () => request<TestDefinition[]>("/api/test-definitions"),
  });
}

export function useSaveTestDefinition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; steps: StressStep[] }) =>
      post<TestDefinition>("/api/test-definitions", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["test-definitions"] });
    },
  });
}

export function useUpdateTestDefinition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name, steps }: { id: number; name?: string; steps?: StressStep[] }) =>
      patch<{ ok: boolean }>(`/api/test-definitions/${id}`, { name, steps }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["test-definitions"] });
    },
  });
}

export function useDeleteTestDefinition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => del<{ ok: boolean }>(`/api/test-definitions/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["test-definitions"] });
    },
  });
}

/** Runs a saved definition exactly like a freshly-built test -- same
 *  StressRun result, so it shows up in the same run-history list below. */
export function useRunTestDefinition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => post<StressRun>(`/api/test-definitions/${id}/run`),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["stress-tests"] });
      qc.setQueryData(["stress-test", run.id], run);
    },
  });
}