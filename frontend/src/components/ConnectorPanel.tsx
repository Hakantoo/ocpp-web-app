/** The connector panel: one instrument module per socket.
 *
 *    Start  -> begins charging, or resumes a held session
 *    Stop   -> holds at 0 W; the transaction stays open, the meter freezes,
 *              and the connector latch releases so the car can leave
 *    End    -> closes the transaction outright; shown alongside Stop while
 *              a session is open, but withheld entirely while faulted --
 *              the charger itself mostly refuses it during a fault, and the
 *              backend now refuses it outright too
 *
 *  Unplugging also ends a session, which is how a driver actually finishes
 *  charging day to day. The latch makes that safe: you cannot pull the cable
 *  while power is flowing, so an unplug always means charging was
 *  deliberately stopped first.
 *
 *  The meter tape along the bottom is the piece worth looking at. It plots the
 *  energy register over the session, and when the connector is held at 0 W the
 *  trace visibly flatlines under a hatched HOLD band. That plateau is the
 *  product's core idea made visible: pausing freezes the register rather than
 *  resetting it, which is exactly why resuming needs no arithmetic.
 */

import { Link } from "react-router-dom";
import {
  AlertTriangle,
  CreditCard,
  Lock,
  Pause,
  PlugZap,
  Play,
  Square,
  Zap,
} from "lucide-react";

import { duration, kwh, pct } from "../lib/format";
import {
  CONNECTOR_MEANING,
  SIGNAL_BG,
  SIGNAL_HEX,
  connectorSignal,
} from "../lib/status";
import type { ConnectorOverview, SeriesPoint } from "../lib/types";
import { Button, Chip, Panel, Readout, cx } from "./ui";

interface Props {
  connector: ConnectorOverview;
  trace?: SeriesPoint[];
  onStart?: () => void;
  onStop?: () => void;
  onEnd?: () => void;
  // Accepted for compatibility with callers that wire the current-limit
  // slider. Unused here; harmless when omitted.
  onLimit?: (amps: number) => void;
  busy?: string | null;
}

export function ConnectorPanel({
  connector,
  trace = [],
  onStart,
  onStop,
  onEnd,
  busy,
}: Props) {
  const signal = connectorSignal(connector.status);
  const state = connector.session_state;
  const offline = !connector.is_online;
  const charging = state === "ACTIVE" || connector.status === "Charging";
  const faulted = state === "FAULTED" || connector.status === "Faulted";
  // End works on a faulted session -- the transaction is still open and
  // closable exactly like a normal one. Stop (pause) is not offered while
  // faulted: installing a 0 W profile on a connector that is already not
  // delivering is meaningless, and the backend rejects it anyway.
  const locked = Boolean(connector.cable_locked);
  // Resuming a paused session needs no second tap: the authorisation lasts
  // until the cable comes out. A faulted session was not paused by us and
  // has no such carry-over -- this is unrelated to the register freezing.
  const canStart = Boolean(connector.authorized_id_tag) || state === "PAUSED";
  // Start is not gated on our reading of the connector. Our view can be
  // stale, and the charger is the authority on whether a cable is in -- it
  // either opens a transaction or refuses, and either answer is worth
  // seeing. Refusing here would put nothing on the wire at all.
  // Showing which card is in play, when we know it. Whether one is *required*
  // is a per-charger setting, and the charger is the authority on the answer,
  // so Start is never disabled here -- an attempt that needs a card comes back
  // with the reason.
  const authorized = Boolean(connector.authorized_id_tag);

  return (
    <Panel className="animate-rise overflow-hidden">
      {/* Signal bar: the fastest possible read on state, before any text. */}
      <div className={cx("h-1 w-full", SIGNAL_BG[signal], offline && "opacity-30")} />

      <div className="flex items-start justify-between gap-3 px-4 pt-3.5">
        <div className="min-w-0">
          <p className="eyebrow mb-1">
            {connector.charge_point_label ?? connector.charge_point_id}
          </p>
          <h3 className="flex items-baseline gap-2 text-sm font-semibold text-ink">
            <span className="tnum text-ink-dim">
              {connector.charge_point_id}·{connector.connector_id}
            </span>
          </h3>
        </div>
        <Chip signal={signal} pip>
          {connector.status}
        </Chip>
      </div>

      <p className="px-4 pb-3 pt-1.5 text-xs text-ink-faint">
        {offline
          ? "Charger offline — last known state unavailable"
          : CONNECTOR_MEANING[connector.status]}
      </p>

      {connector.error_code !== "NoError" && (
        <p className="mx-4 mb-3 flex items-center gap-1.5 rounded-md border border-signal-fault/30 bg-signal-fault/10 px-2 py-1 text-xs text-signal-fault">
          <AlertTriangle size={12} /> {connector.error_code}
        </p>
      )}

      {/* Readouts */}
      <div className="grid grid-cols-3 gap-3 border-t border-line px-4 py-3">
        <Readout
          label="Delivered"
          value={
            connector.session_energy_wh != null
              ? kwh(connector.session_energy_wh)
              : "—"
          }
          unit="kWh"
          signal={state === "ACTIVE" ? "live" : undefined}
        />
        <Readout
          label="Battery"
          value={connector.current_soc != null ? pct(connector.current_soc, 1) : "—"}
        />
        <Readout
          label="Charging"
          value={
            connector.session_active_seconds != null
              ? duration(connector.session_active_seconds)
              : "—"
          }
        />
      </div>

      {/* Vehicle and latch */}
      <div className="flex items-center gap-2 border-t border-line px-4 py-2.5 text-xs">
        <PlugZap size={13} className="shrink-0 text-ink-faint" />
        <span className="truncate text-ink-dim">
          {connector.vehicle_name ??
            (connector.session_id
              ? "Vehicle unknown until a card is used"
              : "No vehicle connected")}
        </span>
        {authorized && !locked ? (
          <span
            className="ml-auto flex shrink-0 items-center gap-1 text-signal-live"
            title={`Authorised by ${connector.authorized_id_tag}`}
          >
            <CreditCard size={11} /> {connector.authorized_id_tag}
          </span>
        ) : locked ? (
          <span
            className="ml-auto flex shrink-0 items-center gap-1 text-signal-live"
            title="The latch is engaged while power is flowing. Press Stop to release the cable."
          >
            <Lock size={11} /> Locked
          </span>
        ) : (
          connector.max_power_kw != null && (
            <span className="tnum ml-auto shrink-0 text-ink-faint">
              {connector.max_power_kw} kW max
            </span>
          )
        )}
      </div>

      <MeterTape
        trace={trace}
        frozenReason={
          state === "PAUSED" ? "paused" : faulted ? "faulted" : null
        }
        signal={signal}
      />

      {/* Controls */}
      <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
        {faulted ? (
          <span className="flex items-center gap-1.5 text-xs text-signal-fault">
            <AlertTriangle size={13} />
            Faulted — waiting for the charger to clear it
          </span>
        ) : charging ? (
          <Button variant="hold" onClick={onStop} busy={busy === "stop"}>
            <Pause size={13} /> Stop
          </Button>
        ) : (
          <Button
            variant="primary"
            onClick={onStart}
            busy={busy === "start"}
            disabled={offline || !canStart}
            title={
              offline
                ? "The charger is offline"
                : !canStart
                  ? "Present an RFID card at the charger first"
                  : undefined
            }
          >
            {state === "PAUSED" ? <Play size={13} /> : <Zap size={13} />}
            {state === "PAUSED" ? "Resume" : "Start"}
          </Button>
        )}

        {(charging || state === "PAUSED") && (
          <Button variant="danger" onClick={onEnd} busy={busy === "end"}>
            <Square size={12} /> End
          </Button>
        )}

        {state === "PAUSED" && (
          <span className="text-xs text-ink-faint">
            Cable released — unplug to finish
          </span>
        )}

        {connector.session_id && (
          <Link
            to={`/sessions/${connector.session_id}`}
            className="ml-auto text-xs text-ink-faint underline-offset-4 hover:text-ink hover:underline"
          >
            Session {connector.session_id}
          </Link>
        )}
      </div>
    </Panel>
  );
}

/** The meter tape.
 *
 *  Drawn as a raw SVG polyline rather than a chart component: at this size a
 *  charting library would add axes, tooltips and legends that all have to be
 *  turned off again. The register is monotonic, so the trace only ever climbs
 *  or goes flat — and flat is the interesting part.
 */
function MeterTape({
  trace,
  frozenReason,
  signal,
}: {
  trace: SeriesPoint[];
  frozenReason: "paused" | "faulted" | null;
  signal: ReturnType<typeof connectorSignal>;
}) {
  const W = 320;
  const H = 44;

  if (trace.length < 2) {
    return (
      <div className="relative h-[44px] border-t border-line bg-panel/60">
        <p className="absolute inset-0 grid place-items-center text-eyebrow uppercase text-ink-faint">
          No meter data yet
        </p>
      </div>
    );
  }

  const values = trace.map((p) => p.v);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;

  const points = trace
    .map((p, i) => {
      const x = (i / (trace.length - 1)) * W;
      const y = H - 4 - ((p.v - min) / span) * (H - 10);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const colour = SIGNAL_HEX[signal];

  return (
    <div className="relative border-t border-line bg-panel/60">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="block h-[44px] w-full"
        role="img"
        aria-label={
          frozenReason ? "Energy register held flat" : "Energy register climbing"
        }
      >
        <defs>
          <linearGradient id={`fill-${signal}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={colour} stopOpacity="0.28" />
            <stop offset="100%" stopColor={colour} stopOpacity="0" />
          </linearGradient>
        </defs>
        <polygon points={`0,${H} ${points} ${W},${H}`} fill={`url(#fill-${signal})`} />
        <polyline
          points={points}
          fill="none"
          stroke={colour}
          strokeWidth="1.5"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>

      {frozenReason && (
        <div className="hatch absolute inset-0 flex items-center justify-center">
          <span
            className={cx(
              "rounded border px-2 py-0.5 text-eyebrow uppercase",
              frozenReason === "faulted"
                ? "border-signal-fault/40 bg-panel/90 text-signal-fault"
                : "border-signal-hold/40 bg-panel/90 text-signal-hold",
            )}
          >
            {frozenReason === "faulted"
              ? "Faulted · register frozen"
              : "Hold · register frozen"}
          </span>
        </div>
      )}
    </div>
  );
}