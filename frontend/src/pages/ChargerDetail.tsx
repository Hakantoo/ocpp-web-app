/** One charger in full.
 *
 *  Settings are limited to what the dashboard actually owns, not what the
 *  hardware reports about itself: display name, heartbeat interval,
 *  registration status, whether charging profiles are supported, whether a
 *  card is required before Start, and the artificial reply delay used for
 *  testing. Vendor, model, firmware and similar facts stay read-only here,
 *  since editing them would make the record a description of nothing --
 *  they belong to the charger, not to us.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Check, Pencil, X } from "lucide-react";
import {
  CalendarClock,
  ChevronDown,
  Cpu,
  Power,
  Radio,
  ShieldCheck,
} from "lucide-react";

import {
  useChangeConfiguration,
  useDiagnosticsFiles,
  useChargePoint,
  useChargerCommand,
  useUpdateChargePoint,
  useUptimeSummary,
  useUptimeTimeline,
} from "../lib/api";
import { datetime, duration, since } from "../lib/format";
import { CONNECTOR_MEANING, connectorSignal } from "../lib/status";
import type { ChargePoint } from "../lib/types";
import { UptimeBarChart } from "../components/charts";
import {
  Button,
  Chip,
  cx,
  ErrorNote,
  Field,
  Input,
  Modal,
  Note,
  Panel,
  PanelHeader,
  Select,
  Skeleton,
  Table,
  Td,
  Th,
} from "../components/ui";

export function ChargerDetail() {
  const { identity = "" } = useParams();
  const { data: cp, isLoading, error } = useChargePoint(identity);
  const [editing, setEditing] = useState(false);

  if (isLoading) return <Skeleton className="h-72" />;
  if (error) return <ErrorNote message={(error as Error).message} />;
  if (!cp) return null;

  return (
    <div className="space-y-5">
      <Link
        to="/chargers"
        className="inline-flex items-center gap-1.5 text-xs text-ink-faint hover:text-ink"
      >
        <ArrowLeft size={13} /> All chargers
      </Link>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="eyebrow mb-1.5">{cp.identity}</p>
          <h1 className="text-2xl font-semibold tracking-tight">
            {cp.label ?? cp.identity}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          <Chip signal={cp.live ? "live" : "idle"} pip={Boolean(cp.live)}>
            {cp.live ? "Online" : "Offline"}
          </Chip>
          <Button onClick={() => setEditing(true)}>
            <Pencil size={12} /> Settings
          </Button>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel className="lg:col-span-2">
          <PanelHeader eyebrow="Sockets" title="Connectors" />
          <Table>
            <thead>
              <tr>
                <Th>#</Th>
                <Th>State</Th>
                <Th>Meaning</Th>
                <Th className="text-right">Max</Th>
              </tr>
            </thead>
            <tbody>
              {(cp.connectors ?? [])
                .filter((c) => c.connector_id > 0)
                .map((c) => (
                  <tr key={c.id}>
                    <Td className="tnum text-ink">{c.connector_id}</Td>
                    <Td>
                      <Chip signal={connectorSignal(c.status)}>{c.status}</Chip>
                    </Td>
                    <Td>{CONNECTOR_MEANING[c.status]}</Td>
                    <Td className="tnum text-right">
                      {c.max_power_kw ? `${c.max_power_kw} kW` : "—"}
                    </Td>
                  </tr>
                ))}
            </tbody>
          </Table>
        </Panel>

        <Panel>
          <PanelHeader eyebrow="Identity" title="Reported by the charger" />
          <dl className="divide-y divide-line/60 text-sm">
            {[
              ["Identity", cp.identity],
              ["Vendor", cp.vendor],
              ["Model", cp.model],
              ["Serial", cp.serial_number],
              ["Firmware", cp.firmware_version],
              ["Registration", cp.registration_status],
              [
                "Card before Start",
                cp.require_card_before_start ? "Required" : "Not required",
              ],
              [
                "Reply delay",
                cp.response_delay_s ? `${cp.response_delay_s}s` : "None",
              ],
              ["Heartbeat", `${cp.heartbeat_interval}s`],
              ["Last seen", cp.last_seen ? `${since(cp.last_seen)} ago` : "—"],
            ].map(([label, value]) => (
              <div key={label} className="flex justify-between gap-3 px-4 py-2">
                <dt className="text-ink-faint">{label}</dt>
                <dd className="tnum truncate text-ink-dim">{value ?? "—"}</dd>
              </div>
            ))}
          </dl>
        </Panel>
      </div>

      <UptimePanel identity={cp.identity} />

      <CommandPanel cp={cp} />

      <ConfigurationPanel cp={cp} />

      <DiagnosticsPanel identity={cp.identity} />

      {editing && <SettingsEditor cp={cp} onClose={() => setEditing(false)} />}
    </div>
  );
}

/** Every OCPP command available against this charger, grouped by what an
 *  operator is actually trying to do rather than dumped in one flat row.
 *  Groups collapse to keep the page short; the ones you reach for daily
 *  (diagnostics, remote triggers) start open, the rest start closed.
 */
function CommandPanel({ cp }: { cp: ChargePoint }) {
  const getConfiguration = useChargerCommand("get-configuration");
  const diagnostics = useChargerCommand("diagnostics");
  const trigger = useChargerCommand("trigger");
  const reset = useChargerCommand("reset");
  const clearCache = useChargerCommand("clear-cache");
  const unlock = useChargerCommand("unlock");
  const getLocalListVersion = useChargerCommand("get-local-list-version");
  const sendLocalList = useChargerCommand("send-local-list");
  const reserveNow = useChargerCommand("reserve-now");
  const cancelReservation = useChargerCommand("cancel-reservation");
  const compositeSchedule = useChargerCommand("composite-schedule");
  const updateFirmware = useChargerCommand("update-firmware");
  const dataTransfer = useChargerCommand("data-transfer");
  const [reply, setReply] = useState<{ ok: boolean; text: string } | null>(null);

  // Small inputs for the commands that need a real value rather than a fixed
  // constant -- a bare button would otherwise send a made-up reservation id
  // or an empty firmware location, which is worse than asking.
  const [reservationId, setReservationId] = useState("1");
  const [reserveIdTag, setReserveIdTag] = useState("");
  const [reserveMinutes, setReserveMinutes] = useState("60");
  const [firmwareUrl, setFirmwareUrl] = useState("");
  const [localListVersion, setLocalListVersion] = useState("1");
  const [vendorId, setVendorId] = useState("");

  async function send(
    label: string,
    run: () => Promise<Record<string, unknown>>,
  ) {
    setReply(null);
    try {
      const result = await run();
      setReply({ ok: true, text: `${label} → ${JSON.stringify(result)}` });
    } catch (err) {
      setReply({
        ok: false,
        text: `${label} → ${err instanceof Error ? err.message : "no answer"}`,
      });
    }
  }

  const identity = cp.identity;
  const busy =
    getConfiguration.isPending ||
    diagnostics.isPending ||
    trigger.isPending ||
    reset.isPending ||
    clearCache.isPending ||
    unlock.isPending ||
    getLocalListVersion.isPending ||
    sendLocalList.isPending ||
    reserveNow.isPending ||
    cancelReservation.isPending ||
    compositeSchedule.isPending ||
    updateFirmware.isPending ||
    dataTransfer.isPending;

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="Send"
        title="Commands"
        right={
          <span className="text-xs text-ink-faint">
            replies are shown exactly as received
          </span>
        }
      />

      <div className="divide-y divide-line">
        <CommandGroup
          icon={Radio}
          title="Diagnostics & status"
          description="Ask the charger to report in now, or pull its configuration and logs."
          defaultOpen
        >
          <CommandRow
            label="GetConfiguration"
            hint="Reads every setting the charger exposes."
          >
            <Button
              variant="primary"
              busy={busy}
              onClick={() =>
                send("GetConfiguration", () =>
                  getConfiguration.mutateAsync({ identity }),
                )
              }
            >
              Send
            </Button>
          </CommandRow>

          <CommandRow
            label="TriggerMessage"
            hint="Ask for one message now instead of waiting for its own schedule."
          >
            <div className="flex flex-wrap gap-2">
              <Button
                busy={busy}
                onClick={() =>
                  send("TriggerMessage(MeterValues)", () =>
                    trigger.mutateAsync({
                      identity,
                      body: { requested_message: "MeterValues", connector_id: 1 },
                    }),
                  )
                }
              >
                MeterValues
              </Button>
              <Button
                busy={busy}
                onClick={() =>
                  send("TriggerMessage(StatusNotification)", () =>
                    trigger.mutateAsync({
                      identity,
                      body: {
                        requested_message: "StatusNotification",
                        connector_id: 1,
                      },
                    }),
                  )
                }
              >
                StatusNotification
              </Button>
              <Button
                busy={busy}
                onClick={() =>
                  send("TriggerMessage(Heartbeat)", () =>
                    trigger.mutateAsync({
                      identity,
                      body: { requested_message: "Heartbeat" },
                    }),
                  )
                }
              >
                Heartbeat
              </Button>
            </div>
          </CommandRow>

          <CommandRow
            label="GetDiagnostics"
            hint="Uploads logs to this CSMS. Files appear below once received."
          >
            <Button
              busy={busy}
              onClick={() =>
                send("GetDiagnostics", () =>
                  // No location: the backend fills in its own reachable
                  // address, which is the one the charger can actually
                  // upload to.
                  diagnostics.mutateAsync({ identity, body: {} }),
                )
              }
            >
              Send
            </Button>
          </CommandRow>

          <CommandRow
            label="GetCompositeSchedule"
            hint="What connector 1 will actually deliver over the next hour."
          >
            <Button
              busy={busy}
              onClick={() =>
                send("GetCompositeSchedule(1, 3600s)", () =>
                  compositeSchedule.mutateAsync({
                    identity,
                    body: { connector_id: 1, duration: 3600 },
                  }),
                )
              }
            >
              Send
            </Button>
          </CommandRow>
        </CommandGroup>

        <CommandGroup
          icon={Power}
          title="Maintenance"
          description="Unlock a stuck connector, forget cached cards, or restart the charger."
          defaultOpen
        >
          <CommandRow label="UnlockConnector" hint="Releases connector 1's latch.">
            <Button
              busy={busy}
              onClick={() =>
                send("UnlockConnector", () =>
                  unlock.mutateAsync({ identity, body: { connector_id: 1 } }),
                )
              }
            >
              Send
            </Button>
          </CommandRow>

          <CommandRow
            label="ClearCache"
            hint="Forgets locally cached authorization decisions."
          >
            <Button
              busy={busy}
              onClick={() =>
                send("ClearCache", () => clearCache.mutateAsync({ identity }))
              }
            >
              Send
            </Button>
          </CommandRow>

          <CommandRow
            label="Reset"
            hint="Soft restarts the OCPP stack; Hard power-cycles the unit."
          >
            <div className="flex flex-wrap gap-2">
              <Button
                variant="hold"
                busy={busy}
                onClick={() =>
                  send("Reset(Soft)", () =>
                    reset.mutateAsync({ identity, body: { type: "Soft" } }),
                  )
                }
              >
                Soft
              </Button>
              <Button
                variant="danger"
                busy={busy}
                onClick={() =>
                  send("Reset(Hard)", () =>
                    reset.mutateAsync({ identity, body: { type: "Hard" } }),
                  )
                }
              >
                Hard
              </Button>
            </div>
          </CommandRow>
        </CommandGroup>

        <CommandGroup
          icon={CalendarClock}
          title="Reservations"
          description="Hold connector 1 for a specific card, or release an existing hold."
        >
          <CommandRow label="ReserveNow" wide>
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Id tag" className="w-40">
                <Input
                  value={reserveIdTag}
                  onChange={setReserveIdTag}
                  placeholder="RFID-0001"
                />
              </Field>
              <Field label="Minutes" className="w-24">
                <Input value={reserveMinutes} onChange={setReserveMinutes} />
              </Field>
              <Field label="Reservation id" className="w-28">
                <Input value={reservationId} onChange={setReservationId} />
              </Field>
              <Button
                busy={busy}
                disabled={!reserveIdTag}
                onClick={() => {
                  const expiry = new Date(
                    Date.now() + Number(reserveMinutes || 60) * 60_000,
                  ).toISOString();
                  return send("ReserveNow", () =>
                    reserveNow.mutateAsync({
                      identity,
                      body: {
                        connector_id: 1,
                        expiry_date: expiry,
                        id_tag: reserveIdTag,
                        reservation_id: Number(reservationId),
                      },
                    }),
                  );
                }}
              >
                Reserve
              </Button>
            </div>
          </CommandRow>

          <CommandRow
            label="CancelReservation"
            hint="Releases the reservation id entered above."
          >
            <Button
              busy={busy}
              onClick={() =>
                send("CancelReservation", () =>
                  cancelReservation.mutateAsync({
                    identity,
                    body: { reservation_id: Number(reservationId) },
                  }),
                )
              }
            >
              Cancel
            </Button>
          </CommandRow>
        </CommandGroup>

        <CommandGroup
          icon={ShieldCheck}
          title="Firmware & offline auth list"
          description="Push new firmware, or manage the card list the charger uses while offline."
        >
          <CommandRow label="GetLocalListVersion">
            <Button
              busy={busy}
              onClick={() =>
                send("GetLocalListVersion", () =>
                  getLocalListVersion.mutateAsync({ identity }),
                )
              }
            >
              Send
            </Button>
          </CommandRow>

          <CommandRow
            label="SendLocalList"
            hint="Sends an empty Full list, clearing the charger's offline list."
            wide
          >
            <div className="flex flex-wrap items-end gap-3">
              <Field label="List version" className="w-28">
                <Input value={localListVersion} onChange={setLocalListVersion} />
              </Field>
              <Button
                busy={busy}
                onClick={() =>
                  send("SendLocalList(Full, empty)", () =>
                    sendLocalList.mutateAsync({
                      identity,
                      body: {
                        list_version: Number(localListVersion),
                        update_type: "Full",
                        local_authorization_list: [],
                      },
                    }),
                  )
                }
              >
                Send
              </Button>
            </div>
          </CommandRow>

          <CommandRow
            label="UpdateFirmware"
            hint="No reply confirms success -- watch FirmwareStatusNotification."
            wide
          >
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Location (URL)" className="w-80">
                <Input
                  value={firmwareUrl}
                  onChange={setFirmwareUrl}
                  placeholder="https://example.com/firmware.bin"
                />
              </Field>
              <Button
                variant="danger"
                busy={busy}
                disabled={!firmwareUrl}
                onClick={() =>
                  send("UpdateFirmware", () =>
                    updateFirmware.mutateAsync({
                      identity,
                      body: {
                        location: firmwareUrl,
                        retrieve_date: new Date().toISOString(),
                      },
                    }),
                  )
                }
              >
                Send
              </Button>
            </div>
          </CommandRow>
        </CommandGroup>

        <CommandGroup
          icon={Cpu}
          title="Vendor extension"
          description="Send raw DataTransfer to whatever a specific charger model recognises."
        >
          <CommandRow
            label="DataTransfer"
            hint="Chargers that do not recognise the vendor id reply UnknownVendorId."
            wide
          >
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Vendor id" className="w-52">
                <Input
                  value={vendorId}
                  onChange={setVendorId}
                  placeholder="com.example"
                />
              </Field>
              <Button
                busy={busy}
                disabled={!vendorId}
                onClick={() =>
                  send("DataTransfer", () =>
                    dataTransfer.mutateAsync({
                      identity,
                      body: { vendor_id: vendorId },
                    }),
                  )
                }
              >
                Send
              </Button>
            </div>
          </CommandRow>
        </CommandGroup>
      </div>

      {reply && (
        <div className="border-t border-line px-4 py-3">
          <Note tone={reply.ok ? "ok" : "error"}>
            <span className="tnum break-all">{reply.text}</span>
          </Note>
        </div>
      )}
    </Panel>
  );
}

/** One collapsible group of related commands. Collapsed groups still show
 *  their description, so the panel reads as a table of contents even before
 *  anything is expanded. */
function CommandGroup({
  icon: Icon,
  title,
  description,
  defaultOpen = false,
  children,
}: {
  icon: typeof Radio;
  title: string;
  description: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-panel-high/40"
      >
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-line bg-panel-high text-ink-dim">
          <Icon size={15} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-ink">{title}</span>
          <span className="block truncate text-xs text-ink-faint">
            {description}
          </span>
        </span>
        <ChevronDown
          size={16}
          className={cx(
            "shrink-0 text-ink-faint transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {open && (
        <div className="space-y-3 bg-panel/40 px-4 pb-4 pt-1">{children}</div>
      )}
    </div>
  );
}

/** One command inside a group: a label, an optional one-line hint, and
 *  whatever controls it needs, aligned so a scan down the group reads as a
 *  list rather than a wall of buttons. */
function CommandRow({
  label,
  hint,
  wide = false,
  children,
}: {
  label: string;
  hint?: string;
  /** Rows with several inputs (ReserveNow, and similar) need the full width
   *  of the panel rather than squeezing beside the label -- that squeeze is
   *  exactly what split a label from its own input onto separate lines. */
  wide?: boolean;
  children: ReactNode;
}) {
  if (wide) {
    return (
      <div className="space-y-2.5 rounded-lg border border-line bg-panel px-3 py-2.5">
        <div>
          <p className="font-mono text-xs font-medium text-ink">{label}</p>
          {hint && <p className="mt-0.5 text-xs text-ink-faint">{hint}</p>}
        </div>
        {children}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-line bg-panel px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <p className="font-mono text-xs font-medium text-ink">{label}</p>
        {hint && <p className="mt-0.5 text-xs text-ink-faint">{hint}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

/** One reliability figure with real visual weight: a big number plus a
 *  slim fill bar underneath, colored by how good the number actually is
 *  rather than always the same neutral tone. A bare "97.8%" reads as a
 *  caption; the bar makes the gap to 100% something you see, not calculate.
 */
function UptimeGauge({
  label,
  percent,
}: {
  label: string;
  percent: number | null;
}) {
  const tone: "ink-faint" | "live" | "hold" | "fault" =
    percent == null ? "ink-faint" : percent >= 80 ? "live" : percent >= 50 ? "hold" : "fault";
  const TEXT_TONE = {
    "ink-faint": "text-ink-faint",
    live: "text-signal-live",
    hold: "text-signal-hold",
    fault: "text-signal-fault",
  } as const;
  const BAR_TONE = {
    "ink-faint": "bg-ink-faint",
    live: "bg-signal-live",
    hold: "bg-signal-hold",
    fault: "bg-signal-fault",
  } as const;

  return (
    <div className="rounded-lg border border-line bg-panel-high/40 px-3 py-2.5">
      <p className="eyebrow mb-1">{label}</p>
      <p className={cx("tnum text-2xl font-semibold leading-none", TEXT_TONE[tone])}>
        {percent != null ? `${percent}%` : "—"}
      </p>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-ink-faint/10">
        <div
          className={cx("h-full rounded-full transition-all", BAR_TONE[tone])}
          style={{ width: `${percent ?? 0}%` }}
        />
      </div>
    </div>
  );
}

/** The headline sentence, plus both reliability gauges. Shared above the two
 *  chart panels since neither is specific to one window. */
function UptimePanel({ identity }: { identity: string }) {
  const { data: summary } = useUptimeSummary(identity);
  const { data: window48 } = useUptimeTimeline(identity, "48h");
  const { data: window7d } = useUptimeTimeline(identity, "7d");

  const streak = summary?.streak;

  return (
    <Panel>
      <PanelHeader eyebrow="Connection" title="Uptime" />
      <div className="px-4 py-3">
        {/* The headline sentence: the one thing worth reading at a glance. */}
        <p className="text-sm text-ink">
          {streak?.seconds == null ? (
            <span className="text-ink-faint">No connection history yet</span>
          ) : streak.connected ? (
            <>
              <span className="text-signal-live">●</span> Connected — up for{" "}
              <span className="tnum font-medium">{duration(streak.seconds)}</span>
            </>
          ) : (
            <>
              <span className="text-signal-fault">●</span> Offline — last seen{" "}
              <span className="tnum font-medium">{duration(streak.seconds)}</span>{" "}
              ago
            </>
          )}
        </p>

        {/* Both windows' reliability, always both. Each gets real visual
            weight -- a big number plus a fill bar -- rather than sitting as
            a quiet inline caption easy to skim past. */}
        <div className="mt-3 grid grid-cols-2 gap-3">
          <UptimeGauge label="Last 48 hours" percent={summary?.percent_48h ?? null} />
          <UptimeGauge label="Last 7 days" percent={summary?.percent_7d ?? null} />
        </div>
      </div>

      {/* Two horizons, matching the energy charts on the Overview page: real
          connect/disconnect segments as vertical bars, not fixed hourly or
          daily buckets. A segment's height is its own real duration and its
          tooltip shows the real moment it started, rather than rounding
          everything into evenly-spaced boxes the way a bucketed chart would. */}
      <div className="grid gap-4 border-t border-line px-4 py-4 xl:grid-cols-2">
        <Panel>
          <PanelHeader
            eyebrow="Last 48 hours"
            title="Connection history"
            right={<span className="text-xs text-ink-faint">real segments</span>}
          />
          <div className="px-2 py-4">
            <UptimeBarChart segments={window48?.segments ?? []} windowHours={48} />
          </div>
        </Panel>

        <Panel>
          <PanelHeader eyebrow="Last 7 days" title="Connection history" />
          <div className="px-2 py-4">
            <UptimeBarChart segments={window7d?.segments ?? []} windowHours={168} />
          </div>
        </Panel>
      </div>
    </Panel>
  );
}

/** Files the charger has uploaded after a GetDiagnostics.
 *
 *  The upload happens out of band -- the charger PUTs the archive straight to
 *  the CSMS, not over OCPP -- so this list is the only place the result of the
 *  command actually appears. Empty until a charger completes an upload.
 */
function DiagnosticsPanel({ identity }: { identity: string }) {
  const { data } = useDiagnosticsFiles(identity);
  if (!data?.length) return null;

  return (
    <Panel>
      <PanelHeader
        eyebrow="Uploads"
        title="Diagnostics files"
        right={
          <span className="text-xs text-ink-faint">
            uploaded by chargers to this CSMS
          </span>
        }
      />
      <Table>
        <thead>
          <tr>
            <Th>File</Th>
            <Th className="text-right">Size</Th>
            <Th>Received</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {data.map((f) => (
            <tr key={f.name}>
              <Td className="tnum break-all text-ink">{f.name}</Td>
              <Td className="tnum text-right">{(f.bytes / 1024).toFixed(1)} KB</Td>
              <Td className="tnum">{datetime(f.received_at)}</Td>
              <Td className="text-right">
                <a
                  href={`/api/diagnostics/${encodeURIComponent(f.name)}`}
                  download
                  className="text-xs text-signal-live hover:underline"
                >
                  Download
                </a>
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Panel>
  );
}

function ConfigurationPanel({ cp }: { cp: ChargePoint }) {
  const change = useChangeConfiguration();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(
    null,
  );

  async function save(key: string) {
    setNote(null);
    try {
      const result = await change.mutateAsync({
        identity: cp.identity,
        key,
        value: draft,
      });
      setEditing(null);
      setNote(
        result.status === "Accepted"
          ? { tone: "ok", text: `${key} set to ${draft}` }
          : { tone: "error", text: `The charger answered ${result.status}` },
      );
    } catch (err) {
      setNote({
        tone: "error",
        text: err instanceof Error ? err.message : "The charger did not answer",
      });
    }
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="OCPP"
        title="Configuration keys"
        right={
          <span className="text-xs text-ink-faint">
            changes are sent to the charger
          </span>
        }
      />
      {note && (
        <div className="px-4 pt-3">
          <Note tone={note.tone}>{note.text}</Note>
        </div>
      )}
      <Table>
        <thead>
          <tr>
            <Th>Key</Th>
            <Th>Value</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {(cp.configuration ?? []).map((entry) => {
            const locked = Boolean(entry.readonly);
            const isEditing = editing === entry.key;
            return (
              <tr key={entry.key}>
                <Td className="text-ink">
                  {entry.key}
                  {locked && (
                    <span className="ml-2 text-eyebrow uppercase text-ink-faint">
                      read only
                    </span>
                  )}
                </Td>
                <Td className="tnum break-all">
                  {isEditing ? (
                    <Input value={draft} onChange={setDraft} />
                  ) : (
                    (entry.value ?? "—")
                  )}
                </Td>
                <Td className="text-right">
                  {locked ? null : isEditing ? (
                    <div className="flex justify-end gap-1.5">
                      <Button
                        variant="primary"
                        busy={change.isPending}
                        onClick={() => save(entry.key)}
                      >
                        <Check size={12} />
                      </Button>
                      <Button onClick={() => setEditing(null)}>
                        <X size={12} />
                      </Button>
                    </div>
                  ) : (
                    <Button
                      onClick={() => {
                        setEditing(entry.key);
                        setDraft(entry.value ?? "");
                        setNote(null);
                      }}
                    >
                      <Pencil size={12} />
                    </Button>
                  )}
                </Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
      {!cp.configuration?.length && (
        <p className="px-4 py-3 text-xs text-ink-faint">
          Nothing recorded yet. Keys appear once the charger reports them.
        </p>
      )}
    </Panel>
  );
}

export function SettingsEditor({ cp, onClose }: { cp: ChargePoint; onClose: () => void }) {
  const update = useUpdateChargePoint();
  const [label, setLabel] = useState(cp.label ?? "");
  const [heartbeat, setHeartbeat] = useState(String(cp.heartbeat_interval));
  const [registration, setRegistration] = useState(cp.registration_status);
  const [profiles, setProfiles] = useState(
    Boolean(cp.supports_charging_profiles),
  );
  const [requireCard, setRequireCard] = useState(
    Boolean(cp.require_card_before_start),
  );
  const [delay, setDelay] = useState(String(cp.response_delay_s ?? 0));
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setError(null);
    try {
      await update.mutateAsync({
        identity: cp.identity,
        changes: {
          label: label || null,
          heartbeat_interval: Number(heartbeat),
          registration_status: registration,
          supports_charging_profiles: profiles,
          require_card_before_start: requireCard,
          response_delay_s: Math.max(0, Number(delay) || 0),
        },
      });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
    }
  }

  return (
    <Modal
      title={`Settings for ${cp.identity}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} busy={update.isPending}>
            Save changes
          </Button>
        </>
      }
    >
      {error && <ErrorNote message={error} />}
      <Note tone="info">
        Vendor, model, serial and firmware come from the charger itself and are
        not editable here.
      </Note>
      <Field label="Name" hint="What this charger is called in the dashboard">
        <Input value={label} onChange={setLabel} placeholder="Garage charger" />
      </Field>
      <Field
        label="Heartbeat interval (seconds)"
        hint="How often the charger checks in. Also how it keeps its clock right."
      >
        <Input type="number" min={1} value={heartbeat} onChange={setHeartbeat} />
      </Field>
      <Field
        label="Registration"
        hint="Pending tells the charger to wait and try booting again, so it never sends heartbeats and may refuse commands. Accepted is what puts it into service."
      >
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
      <Field
        label="Require a card before Start"
        hint="Only works on chargers that send Authorize when a card is read. Many handle the card themselves and never tell the CSMS — switching this on there would block Start with nothing on the wire to explain why."
      >
        <Select
          value={requireCard ? "yes" : "no"}
          onChange={(v: string) => setRequireCard(v === "yes")}
          options={[
            { value: "no", label: "No — Start always available" },
            { value: "yes", label: "Yes — a card must be presented first" },
          ]}
        />
      </Field>
      <Field
        label="Require a card before Start"
        hint="Only works on chargers that send Authorize when a card is read. On a charger that validates cards internally this would block Start with nothing on the wire to explain why, so it is off unless you turn it on."
      >
        <Select
          value={requireCard ? "yes" : "no"}
          onChange={(v: string) => setRequireCard(v === "yes")}
          options={[
            { value: "no", label: "No — Start works straight away" },
            { value: "yes", label: "Yes — a card must be presented first" },
          ]}
        />
      </Field>
      <Field
        label="Reply delay (seconds)"
        hint="Hold every reply to this charger by this many seconds, to test how it behaves when the CSMS is slow. On the hardware tested so far, 2s held reliably but 5s caused a reconnect loop -- the real cause is inside the charger's own firmware and not something we can compute, so nothing here is enforced. Stay at 2s or below unless you have tested higher on this specific unit."
      >
        <Input
          type="number"
          min={0}
          value={delay}
          onChange={setDelay}
        />
      </Field>
      <Field
        label="Supports charging profiles"
        hint="Stop holds the connector at zero using a charging profile. Switch this off only for hardware that cannot do it at all."
      >
        <Select
          value={profiles ? "yes" : "no"}
          onChange={(v: string) => setProfiles(v === "yes")}
          options={[
            { value: "yes", label: "Yes" },
            { value: "no", label: "No" },
          ]}
        />
      </Field>
    </Modal>
  );
}