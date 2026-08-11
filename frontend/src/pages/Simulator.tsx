/** Physical controls for the simulated hardware.
 *
 *  Deliberately separated from the operator pages: everything here is an act
 *  on the charger itself (a cable being connected, a card presented, a fault
 *  occurring), not a command sent to it. Confusing the two is how you end up
 *  with a dashboard that lies about what it can actually do.
 *
 *  Each connector owns its own car and card pickers, because that is what the
 *  choice actually applies to. A page-level picker would imply the setting was
 *  global when it never was.
 *
 *  More than one charger can run at once, each its own independent WebSocket
 *  connection -- exactly how two real chargers would look to the CSMS.
 */

import { useState } from "react";
import {
  Cable,
  CreditCard,
  Lock,
  Plus,
  Settings,
  Trash2,
  TriangleAlert,
  Unplug,
  Zap,
} from "lucide-react";

import {
  useAddSimCharger,
  useChargePoint,
  useRemoveSimCharger,
  useSimChargers,
  useSimCommand,
  useSimConnectionToggle,
  useTags,
  useUpdateChargePoint,
  useUpdateSimSettings,
  useVehicles,
} from "../lib/api";
import { kwh, pct } from "../lib/format";
import { connectorSignal } from "../lib/status";
import type { ConnectorStatus, SimCharger, SimConnector } from "../lib/types";
import {
  Button,
  Chip,
  cx,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Modal,
  Note,
  Panel,
  PanelHeader,
  Readout,
  Select,
} from "../components/ui";

/** What each refusal actually means, in the operator's language rather than
 *  the protocol's. */
const REFUSAL: Record<string, string> = {
  Accepted: "Card accepted — the reader recognised it and let it through",
  Blocked: "Refused — this card is blocked",
  Expired: "Refused — this card has expired",
  Invalid: "Refused — this card is not registered, or the connector is faulted",
  // Covers two different real reasons the backend collapses into the same
  // OCPP status: the card itself already has a session open elsewhere, or
  // (more likely here) this connector already has a different card recorded
  // for its current session and only that same card can be re-presented.
  ConcurrentTx: "Refused — already has a session open, or this connector already has a different card",
};

export function Simulator() {
  const { data, error } = useSimChargers();
  const { data: tags } = useTags();
  const { data: vehicles } = useVehicles();

  if (error) {
    return (
      <Panel>
        <EmptyState
          title="Simulator not reachable"
          hint="Start it with: python -m simulator.main — it listens on :9100 and the dev server proxies /sim to it."
        />
      </Panel>
    );
  }
  if (!data) return null;

  // Cars come from the CSMS, not from a list inside the simulator. One
  // source of truth means a car added in the dashboard is pluggable at once,
  // and a car already in a socket is visibly unavailable rather than
  // silently duplicating itself onto a second connector.
  const carOptions: { value: string; label: string; disabled?: boolean }[] =
    (vehicles ?? []).map((v) => ({
    value: String(v.id),
    label: `${v.name} · ${v.current_soc.toFixed(0)}%${
      v.session_id ? ` · on ${v.charge_point_id}·${v.connector_id}` : ""
    }`,
    disabled: Boolean(v.session_id),
  }));

  // Read the real tag list rather than a hardcoded one, so a card created on
  // the directory page is immediately presentable here.
  const tagOptions = (tags ?? []).map((tag) => ({
    value: tag.id_tag,
    label: `${tag.id_tag}${tag.status === "Accepted" ? "" : ` · ${tag.status}`}${
      tag.session_id ? ` · on ${tag.charge_point_id}·${tag.connector_id}` : ""
    }`,
    disabled: Boolean(tag.session_id),
  }));

  return (
    <div className="space-y-5">
      <div>
        <p className="eyebrow mb-1.5">Bench</p>
        <h1 className="text-2xl font-semibold tracking-tight">Simulator</h1>
        <p className="mt-2 max-w-2xl text-xs text-ink-faint">
          These are physical acts on the charger — connecting a cable, presenting a
          card, injecting a fault. Commands sent <em>to</em> the charger live on the
          overview.
        </p>
      </div>

      <ChargerProvisionForm existingIdentities={data.chargers.map((c) => c.identity)} />

      {data.chargers.map((charger) => (
        <ChargerBench
          key={charger.identity}
          charger={charger}
          existingIdentities={data.chargers.map((c) => c.identity)}
          carOptions={carOptions}
          tagOptions={tagOptions}
        />
      ))}
    </div>
  );
}

/** Creates a genuinely new piece of fake hardware -- provisioned in the CSMS's
 *  own database exactly the way real hardware would be pre-registered, then
 *  connected as its own independent WebSocket the moment it exists. Same
 *  field set a real charger's settings editor manages after connection, plus
 *  the handful of things (connector count, per-connector power, vendor/model
 *  seeding) that only make sense before it has ever booted. */
/** Creates or edits a piece of fake hardware -- the same form either way.
 *  Creating provisions it in the CSMS's own database exactly the way real
 *  hardware would be pre-registered, then connects it as its own independent
 *  WebSocket. Editing reopens this same form pre-filled, with identity and
 *  connector count locked: changing either on a running simulated charger
 *  is not something the object model (or a real charger, for that matter)
 *  supports -- a new identity is a new WebSocket path, and a different
 *  connector count means physically different hardware. Wanting either
 *  means creating a new charger, not editing this one.
 */
function ChargerProvisionForm({
  existingIdentities,
  existing,
  onClose,
}: {
  existingIdentities: string[];
  existing?: SimCharger;
  onClose?: () => void;
}) {
  const add = useAddSimCharger();
  const updateChargePoint = useUpdateChargePoint();
  const updateSimSettings = useUpdateSimSettings();
  const { data: chargePoint } = useChargePoint(existing?.identity ?? "");

  const isEdit = Boolean(existing);
  const [open, setOpen] = useState(isEdit);
  const [identity, setIdentity] = useState(existing?.identity ?? "");
  const [label, setLabel] = useState("");
  const [connectorCount, setConnectorCount] = useState("");
  const [maxPowerKw, setMaxPowerKw] = useState("");
  const [heartbeat, setHeartbeat] = useState("");
  const [registration, setRegistration] = useState("Accepted");
  const [profiles, setProfiles] = useState(true);
  const [requireCard, setRequireCard] = useState(false);
  const [vendor, setVendor] = useState("");
  const [model, setModel] = useState("");
  const [serial, setSerial] = useState("");
  const [firmware, setFirmware] = useState("");
  // Simulator-only runtime knobs -- there is no real-hardware equivalent, so
  // these only ever come from the running charger's current values, never
  // from BootNotification or a settings default.
  const [timeScale, setTimeScale] = useState("");
  const [sampleInterval, setSampleInterval] = useState("");
  const [fullDwell, setFullDwell] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [prefilled, setPrefilled] = useState(false);

  // Pre-fill once the real record has loaded, rather than on every render --
  // otherwise a field the operator is actively editing would keep getting
  // overwritten by the query refetching in the background.
  if (isEdit && chargePoint && existing && !prefilled) {
    setPrefilled(true);
    setLabel(chargePoint.label ?? "");
    setHeartbeat(String(chargePoint.heartbeat_interval));
    setRegistration(chargePoint.registration_status);
    setProfiles(Boolean(chargePoint.supports_charging_profiles));
    setRequireCard(Boolean(chargePoint.require_card_before_start));
    setVendor(chargePoint.vendor ?? "");
    setModel(chargePoint.model ?? "");
    setSerial(chargePoint.serial_number ?? "");
    setFirmware(chargePoint.firmware_version ?? "");
    const firstConnector = chargePoint.connectors?.find((c) => c.connector_id === 1);
    setMaxPowerKw(firstConnector ? String(firstConnector.max_power_kw ?? "") : "");
    setTimeScale(String(existing.time_scale ?? ""));
    setSampleInterval(String(existing.meter_sample_interval ?? ""));
    setFullDwell(String(existing.full_dwell_seconds ?? ""));
  }

  const trimmed = identity.trim();
  const duplicate = !isEdit && trimmed.length > 0 && existingIdentities.includes(trimmed);

  async function submit() {
    setError(null);
    try {
      if (isEdit && existing) {
        await updateChargePoint.mutateAsync({
          identity: existing.identity,
          changes: {
            label: label.trim() || null,
            heartbeat_interval: Number(heartbeat) || 300,
            registration_status: registration,
            supports_charging_profiles: profiles,
            require_card_before_start: requireCard,
          },
        });
        await updateSimSettings.mutateAsync({
          identity: existing.identity,
          time_scale: timeScale ? Number(timeScale) : undefined,
          meter_sample_interval: sampleInterval ? Number(sampleInterval) : undefined,
          max_power_kw: maxPowerKw ? Number(maxPowerKw) : undefined,
          full_dwell_seconds: fullDwell !== "" ? Number(fullDwell) : undefined,
        });
        onClose?.();
      } else {
        await add.mutateAsync({
          identity: trimmed,
          connectors: connectorCount ? Number(connectorCount) : 2,
          label: label.trim() || undefined,
          max_power_kw: maxPowerKw ? Number(maxPowerKw) : undefined,
          heartbeat_interval: heartbeat ? Number(heartbeat) : undefined,
          registration_status: registration,
          supports_charging_profiles: profiles,
          require_card_before_start: requireCard,
          vendor: vendor.trim() || undefined,
          model: model.trim() || undefined,
          serial_number: serial.trim() || undefined,
          firmware_version: firmware.trim() || undefined,
          time_scale: timeScale ? Number(timeScale) : undefined,
          meter_sample_interval: sampleInterval ? Number(sampleInterval) : undefined,
          full_dwell_seconds: fullDwell !== "" ? Number(fullDwell) : undefined,
        });
        setOpen(false);
        setIdentity("");
        setLabel("");
      }
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : `Could not ${isEdit ? "save" : "create"} that charger`,
      );
    }
  }

  if (!isEdit && !open) {
    return (
      <Button variant="primary" onClick={() => setOpen(true)}>
        <Plus size={13} /> Create a charger
      </Button>
    );
  }

  const busy = add.isPending || updateChargePoint.isPending || updateSimSettings.isPending;

  return (
    <Modal
      title={isEdit ? `Edit ${existing?.identity}` : "Create a charger"}
      onClose={() => (isEdit ? onClose?.() : setOpen(false))}
      footer={
        <>
          <Button onClick={() => (isEdit ? onClose?.() : setOpen(false))}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={!isEdit && (!trimmed || duplicate)}
            title={duplicate ? "A charger with that identity already exists" : undefined}
            onClick={submit}
          >
            {isEdit ? "Save" : "Create"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-xs text-ink-faint">
          {isEdit
            ? "Identity and connector count cannot be changed here."
            : "Everything but identity can be left blank."}
        </p>
        {error && <ErrorNote message={error} />}

        {!isEdit && (
          <div>
            <p className="eyebrow mb-2 text-signal-fault">Required</p>
            <Field
              label="Identity"
              hint="What it connects as (ws://.../ocpp/{identity}). Used as the name too if you leave that blank."
            >
              <Input value={identity} onChange={setIdentity} placeholder="CP002" />
            </Field>
          </div>
        )}

        <div>
          <p className="eyebrow mb-2 text-ink-faint">
            {isEdit ? "Identity" : "Optional — same as if the charger has not told us yet"}
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Name" hint="Defaults to the identity">
              <Input value={label} onChange={setLabel} placeholder={trimmed || "Garage charger"} />
            </Field>
            <Field
              label="Connectors"
              hint={isEdit ? "Locked — create a new charger for a different count" : "Defaults to 2"}
            >
              <Input
                type="number"
                min={1}
                max={8}
                value={isEdit ? String(existing?.connectors.length ?? "") : connectorCount}
                onChange={setConnectorCount}
                placeholder="2"
                disabled={isEdit}
              />
            </Field>
            <Field label="Max power per connector (kW)" hint="Defaults to 11">
              <Input
                type="number"
                min={1}
                value={maxPowerKw}
                onChange={setMaxPowerKw}
                placeholder="11"
              />
            </Field>
            <Field label="Heartbeat interval (seconds)" hint="Defaults to 300">
              <Input
                type="number"
                min={1}
                value={heartbeat}
                onChange={setHeartbeat}
                placeholder="300"
              />
            </Field>
            <Field label="Registration">
              <Select
                value={registration}
                onChange={setRegistration}
                options={[
                  { value: "Accepted", label: "Accepted — in service" },
                  { value: "Pending", label: "Pending — waiting to be configured" },
                  { value: "Rejected", label: "Rejected — refuse this charger" },
                ]}
              />
            </Field>
            <Field label="Supports charging profiles">
              <Select
                value={profiles ? "yes" : "no"}
                onChange={(v: string) => setProfiles(v === "yes")}
                options={[
                  { value: "yes", label: "Yes" },
                  { value: "no", label: "No" },
                ]}
              />
            </Field>
            <Field label="Require a card before Start">
              <Select
                value={requireCard ? "yes" : "no"}
                onChange={(v: string) => setRequireCard(v === "yes")}
                options={[
                  { value: "no", label: "No — Start works straight away" },
                  { value: "yes", label: "Yes — a card must be presented first" },
                ]}
              />
            </Field>
          </div>
        </div>

        <div>
          <p className="eyebrow mb-2 text-ink-faint">
            {isEdit ? "Identity reported by the charger" : "Optional — normally reported by the charger itself"}
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Vendor" hint={isEdit ? undefined : "Left blank until it connects and says"}>
              <Input value={vendor} onChange={setVendor} placeholder="VESTEL" />
            </Field>
            <Field label="Model">
              <Input value={model} onChange={setModel} placeholder="EVC04" />
            </Field>
            <Field label="Serial number">
              <Input value={serial} onChange={setSerial} placeholder="1234567890123456" />
            </Field>
            <Field label="Firmware version">
              <Input value={firmware} onChange={setFirmware} placeholder="v3.187.32-1.8.156.0-v7.4.31" />
            </Field>
          </div>
        </div>

        {/* Simulator-only runtime knobs -- no real-hardware equivalent, but
            available at creation too so a fake charger's pace can be set up
            front instead of always starting at the same defaults. Every one
            is a plain attribute the simulator's own loops read fresh each
            tick, so a change here takes effect immediately. */}
        <div>
          <p className="eyebrow mb-2 text-ink-faint">
            {isEdit ? "Simulator timing" : "Optional — simulator timing"}
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Time ×" hint="Defaults to 60 -- how much faster than real time">
              <Input
                value={timeScale}
                onChange={setTimeScale}
                type="number"
                min={1}
                placeholder="60"
              />
            </Field>
            <Field label="Meter sample interval (s)" hint="Defaults to 5">
              <Input
                value={sampleInterval}
                onChange={setSampleInterval}
                type="number"
                min={1}
                placeholder="5"
              />
            </Field>
            <Field
              label="Full-battery dwell (s)"
              hint="Defaults to 4 -- SuspendedEV before the transaction ends"
            >
              <Input
                value={fullDwell}
                onChange={setFullDwell}
                type="number"
                min={0}
                placeholder="4"
              />
            </Field>
          </div>
        </div>

      </div>
    </Modal>
  );
}

function ChargerBench({
  charger,
  existingIdentities,
  carOptions,
  tagOptions,
}: {
  charger: SimCharger;
  existingIdentities: string[];
  carOptions: { value: string; label: string; disabled?: boolean }[];
  tagOptions: { value: string; label: string; disabled?: boolean }[];
}) {
  const goOffline = useSimConnectionToggle("offline");
  const goOnline = useSimConnectionToggle("online");
  const remove = useRemoveSimCharger();
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const busy = goOffline.isPending || goOnline.isPending || remove.isPending;

  // A drop that has not been asked for (a real disconnect the retry loop is
  // already working on) reads differently from one an operator deliberately
  // held down -- the switch and label both need to say which is true rather
  // than collapsing them into one "offline" state.
  const label = charger.connected
    ? "ONLINE"
    : charger.held_offline
      ? "OFFLINE"
      : "RECONNECTING…";
  const isOn = charger.connected;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <p className="eyebrow">{charger.identity}</p>

        {/* A real on/off switch rather than a chip and a separate button --
            green track when connected, red when deliberately held offline,
            amber mid-transition while the retry loop is reconnecting on its
            own. Clicking flips the same held_offline flag the buttons used
            to. */}
        <button
          type="button"
          role="switch"
          aria-checked={isOn}
          disabled={busy}
          onClick={() =>
            charger.held_offline
              ? goOnline.mutateAsync(charger.identity)
              : goOffline.mutateAsync(charger.identity)
          }
          className={cx(
            "relative h-6 w-11 shrink-0 rounded-full border disabled:opacity-60",
            isOn
              ? "border-signal-live/40 bg-gradient-to-r from-signal-live/70 to-signal-live"
              : charger.held_offline
                ? "border-signal-fault/40 bg-gradient-to-r from-signal-fault/70 to-signal-fault"
                : "border-signal-hold/40 bg-gradient-to-r from-signal-hold/50 to-signal-hold/70",
          )}
        >
          <span
            className={cx(
              "absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-panel shadow-panel transition-transform",
              isOn && "translate-x-[18px]",
            )}
          />
        </button>
        <span
          className={cx(
            "tnum text-xs font-medium tracking-wide",
            isOn
              ? "text-signal-live"
              : charger.held_offline
                ? "text-signal-fault"
                : "text-signal-hold",
          )}
        >
          {label}
        </span>

        {/* Reopens the same create form, pre-filled, identity and connector
            count locked -- one form for creating and editing fake hardware,
            not a second editor with its own idea of what matters. */}
        <Button onClick={() => setEditing(true)}>
          <Settings size={13} /> Edit
        </Button>
        <Button variant="danger" onClick={() => setConfirming(true)}>
          <Trash2 size={13} /> Remove
        </Button>
      </div>

      {editing && (
        <ChargerProvisionForm
          existingIdentities={existingIdentities}
          existing={charger}
          onClose={() => setEditing(false)}
        />
      )}

      {confirming && (
        <Modal
          title={`Remove ${charger.identity}?`}
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button onClick={() => setConfirming(false)}>Cancel</Button>
              <Button
                variant="danger"
                busy={remove.isPending}
                onClick={async () => {
                  await remove.mutateAsync(charger.identity);
                  setConfirming(false);
                }}
              >
                <Trash2 size={13} /> Yes, remove it
              </Button>
            </>
          }
        >
          <Note tone="error">
            This disconnects {charger.identity} and removes it from the CSMS.
            Sessions, faults, and uptime history are kept, not destroyed --
            they stay visible under a clearly-marked, frozen version of this
            identity. The connection closes first, then the charger record
            itself is deleted.
          </Note>
        </Modal>
      )}

      {charger.connected && charger.connectors.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2">
          {charger.connectors.map((connector) => (
            <ConnectorBench
              key={connector.connector_id}
              identity={charger.identity}
              connector={connector}
              carOptions={carOptions}
              tagOptions={tagOptions}
            />
          ))}
        </div>
      ) : (
        <Panel>
          <EmptyState
            title={charger.held_offline ? "Held offline" : "Connecting to the CSMS…"}
            hint={
              charger.held_offline
                ? "Press online/offline switch above to let it reconnect."
                : "This charger will show its connectors here once its WebSocket connection is up."
            }
          />
        </Panel>
      )}
    </div>
  );
}

function ConnectorBench({
  identity,
  connector,
  carOptions,
  tagOptions,
}: {
  identity: string;
  connector: SimConnector;
  carOptions: { value: string; label: string; disabled?: boolean }[];
  tagOptions: { value: string; label: string; disabled?: boolean }[];
}) {
  const plug = useSimCommand("plug");
  const unplug = useSimCommand("unplug");
  const swipe = useSimCommand("swipe");
  const fault = useSimCommand("fault");
  const power = useSimCommand("power");

  const [car, setCar] = useState("");
  const [tag, setTag] = useState("");
  // Sync once the real list has data, rather than locking in whatever
  // tagOptions happened to be (often still empty) at the exact instant this
  // component first mounted -- a backend restart without a frontend reload
  // is exactly the case where the list is empty on mount and only becomes
  // real a moment later.
  const [note, setNote] = useState<{ tone: "ok" | "error" | "info"; text: string } | null>(
    null,
  );

  const id = connector.connector_id;
  const faulted = connector.status === "Faulted";

  async function act(
    run: () => Promise<{ ok: boolean; status?: string }>,
    onOk: (result: { ok: boolean; status?: string }) => string,
  ) {
    setNote(null);
    try {
      const result = await run();
      setNote({
        tone: result.ok ? "ok" : "error",
        text: onOk(result),
      });
    } catch (err) {
      setNote({
        tone: "error",
        text: err instanceof Error ? err.message : "That did not work",
      });
    }
  }

  return (
    <Panel className="animate-rise">
      <PanelHeader
        eyebrow={`${identity} · connector ${id}`}
        title={connector.vehicle?.name ?? "Nothing connected"}
        right={
          <Chip signal={connectorSignal(connector.status as ConnectorStatus)}>
            {connector.status}
          </Chip>
        }
      />

      <div className="grid grid-cols-3 gap-3 px-4 py-3">
        <Readout label="Register" value={kwh(connector.meter_wh, 3)} unit="kWh" />
        <Readout
          label="Battery"
          value={connector.vehicle ? pct(connector.vehicle.soc, 1) : "—"}
        />
        <Readout
          label="Transaction"
          value={connector.transaction_id ?? "—"}
          signal={connector.transaction_id ? "live" : undefined}
        />
      </div>

      {connector.paused && (
        <p className="hatch mx-4 mb-3 rounded-md border border-signal-hold/30 px-2 py-1 text-eyebrow uppercase text-signal-hold">
          Held at 0 W — register frozen, cable released
        </p>
      )}

      {/* Pickers belong to the connector they act on. */}
      <div className="grid grid-cols-2 gap-3 border-t border-line px-4 py-3">
        <Select
          value={
            connector.vehicle
              ? String(connector.vehicle.id)
              : car || carOptions.find((o) => !o.disabled)?.value || ""
          }
          onChange={setCar}
          options={carOptions}
          disabled={Boolean(connector.vehicle)}
        />
        <Select
          value={
            connector.active_id_tag
              ? connector.active_id_tag
              : tag || tagOptions.find((o) => !o.disabled)?.value || ""
          }
          onChange={setTag}
          options={tagOptions}
          disabled={Boolean(connector.active_id_tag)}
        />
      </div>

      <div className="flex flex-wrap gap-2 px-4 pb-3">
        <Button
          variant="primary"
          busy={plug.isPending}
          disabled={Boolean(connector.vehicle) || !carOptions.length}
          onClick={() =>
            act(
              () =>
                plug.mutateAsync({
                  identity,
                  connector_id: id,
                  vehicle_id: Number(car || carOptions.find((o) => !o.disabled)?.value),
                }),
              () => "Cable connected — the car is now plugged in and waiting",
            )
          }
        >
          <Cable size={13} /> Plug in
        </Button>

        <Button
          busy={unplug.isPending}
          disabled={!connector.vehicle || connector.cable_locked}
          title={
            connector.cable_locked
              ? "The latch is engaged while power is flowing. Press Stop on the overview first."
              : undefined
          }
          onClick={() =>
            act(
              () => unplug.mutateAsync({ identity, connector_id: id }),
              () => "Cable removed — the connector is free for the next car",
            )
          }
        >
          {connector.cable_locked ? <Lock size={13} /> : <Unplug size={13} />}
          Unplug
        </Button>

        <Button
          busy={swipe.isPending}
          disabled={!connector.vehicle || Boolean(connector.active_id_tag) || !tagOptions.length}
          onClick={() =>
            act(
              () =>
                swipe.mutateAsync({
                  identity,
                  connector_id: id,
                  id_tag: tag || tagOptions.find((o) => !o.disabled)?.value,
                }),
              (result) =>
                REFUSAL[result.status ?? ""] ?? `Card returned ${result.status}`,
            )
          }
        >
          <CreditCard size={13} /> Present card
        </Button>

        <Button
          variant={connector.power_offered ? "primary" : undefined}
          busy={power.isPending}
          disabled={!connector.vehicle}
          onClick={() =>
            act(
              () =>
                power.mutateAsync({
                  identity,
                  connector_id: id,
                  offered: !connector.power_offered,
                }),
              () =>
                connector.power_offered
                  ? "Power withdrawn"
                  : "Power offered",
            )
          }
        >
          <Zap size={13} />
          {connector.power_offered ? "Withdraw power (C)" : "Offer power (C)"}
        </Button>

        <Button
          variant={faulted ? "primary" : "danger"}
          busy={fault.isPending}
          className="ml-auto"
          onClick={() =>
            act(
              () => fault.mutateAsync({ identity, connector_id: id, faulted: !faulted }),
              () =>
                faulted
                  ? "Fault cleared"
                  : "Fault injected",
            )
          }
        >
          <TriangleAlert size={13} />
          {faulted ? "Clear fault" : "Inject fault"}
        </Button>
      </div>

      {note && (
        <div className="px-4 pb-3">
          <Note tone={note.tone}>{note.text}</Note>
        </div>
      )}
    </Panel>
  );
}