/** One session in full: controls, charts, transactions, and the exact OCPP
 *  frames that produced it. The message timeline is deliberately on the same
 *  page as the charts -- when something looks wrong on a graph, the next
 *  question is always "what did the charger actually say?". */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AlertTriangle, ArrowLeft, Pause, Play, Square } from "lucide-react";

import { LogRow } from "../components/LogRow";
import { FaultsTable } from "../components/FaultsTable";

import {
  useEndSession,
  useSession,
  useStartCharging,
  useStopCharging,
} from "../lib/api";
import { datetime, duration, kwh, pct } from "../lib/format";
import { sessionSignal } from "../lib/status";
import { EnergyChart, PowerChart, SocChart } from "../components/charts";
import {
  Button,
  Chip,
  EmptyState,
  ErrorNote,
  Panel,
  PanelHeader,
  Readout,
  Skeleton,
  Table,
  Td,
  Th,
} from "../components/ui";

export function SessionDetail() {
  const { id } = useParams();
  const sessionId = Number(id);
  const { data: session, isLoading, error } = useSession(sessionId);
  const [failure, setFailure] = useState<string | null>(null);
  const [expandedFrames, setExpandedFrames] = useState<Set<number>>(new Set());

  function toggleFrame(id: number) {
    setExpandedFrames((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const start = useStartCharging();
  const stop = useStopCharging();
  const end = useEndSession();

  if (isLoading) return <Skeleton className="h-96" />;
  if (error) return <ErrorNote message={(error as Error).message} />;
  if (!session) return null;

  const series = session.series ?? {};
  const energySeries = series["Energy.Active.Import.Register"] ?? [];
  const powerSeries = series["Power.Active.Import"] ?? [];
  const socSeries = series["SoC"] ?? [];
  const latestSoc = socSeries.at(-1)?.v ?? null;
  const open = ["WAITING", "ACTIVE", "PAUSED", "FAULTED"].includes(session.state);

  // Wall-clock time the cable has been connected, which is not the same as
  // time spent charging -- a session can sit held or idle for hours.
  const elapsedSeconds =
    (Date.parse(session.ended_at ?? new Date().toISOString()) -
      Date.parse(session.plugged_in_at)) /
    1000;

  /** Start doubles as Resume: the backend routes a Start on a held session to
   *  ClearChargingProfile, so one button covers both. */
  async function run(action: "start" | "stop" | "end") {
    setFailure(null);
    try {
      if (action === "start") {
        await start.mutateAsync({
          identity: session!.charge_point_id,
          connector_id: session!.connector_id,
          id_tag: session!.id_tag ?? undefined,
        });
      } else if (action === "stop") {
        await stop.mutateAsync(sessionId);
      } else {
        await end.mutateAsync(sessionId);
      }
    } catch (err) {
      setFailure(err instanceof Error ? err.message : "Command failed");
    }
  }

  return (
    <div className="space-y-5">
      <Link
        to="/sessions"
        className="inline-flex items-center gap-1.5 text-xs text-ink-faint hover:text-ink"
      >
        <ArrowLeft size={13} /> All sessions
      </Link>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="eyebrow mb-1.5">
            {session.charge_point_id} · connector {session.connector_id}
          </p>
          <h1 className="flex items-center gap-3 text-2xl font-semibold tracking-tight">
            Session {session.id}
            <Chip signal={sessionSignal(session.state)} pip={session.state === "ACTIVE"}>
              {session.state}
            </Chip>
          </h1>
        </div>

        {open && (
          <div className="flex flex-wrap items-center gap-3">
            {session.state === "FAULTED" ? (
              <span className="flex items-center gap-1.5 text-xs text-signal-fault">
                <AlertTriangle size={13} />
                Faulted — waiting for the charger to clear it
              </span>
            ) : session.state === "ACTIVE" ? (
              <Button variant="hold" onClick={() => run("stop")} busy={stop.isPending}>
                <Pause size={13} /> Stop
              </Button>
            ) : (
              <Button
                variant="primary"
                onClick={() => run("start")}
                busy={start.isPending}
              >
                <Play size={13} />
                {session.state === "PAUSED" ? "Resume" : "Start"}
              </Button>
            )}
            {/* Stop holds the transaction open. End closes it: the charger
                gets RemoteStopTransaction and the meter reading is final.
                Neither applies while WAITING -- there is no transaction yet.
                End is withheld while FAULTED too -- real hardware mostly
                rejects a remote stop during a fault, and the backend refuses
                it outright now regardless. */}
            {(session.state === "ACTIVE" ||
              session.state === "PAUSED") && (
              <Button
                variant="danger"
                onClick={() => run("end")}
                busy={end.isPending}
              >
                <Square size={12} /> End
              </Button>
            )}
            <span className="text-xs text-ink-faint">
              {session.state === "ACTIVE"
                ? "Cable locked while charging"
                : session.state === "FAULTED"
                  ? "Cable locked; the transaction is still open"
                  : "Unplug the cable to finish"}
            </span>
          </div>
        )}
      </div>

      {failure && <ErrorNote message={failure} />}

      {session.state === "PAUSED" && (
        <p className="hatch rounded-lg border border-signal-hold/30 px-3 py-2 text-xs text-signal-hold">
          Held at zero power. The transaction is still open and the meter is frozen —
          resuming picks up from exactly this reading. The cable is released, so
          unplugging now ends the session.
        </p>
      )}

      {session.state === "FAULTED" && (
        <p className="hatch rounded-lg border border-signal-fault/30 px-3 py-2 text-xs text-signal-fault">
          The charger reported a fault. The transaction is very likely still open on
          its side, so the meter is frozen but nothing else has changed — charging
          resumes on its own once the fault clears. Start, Stop, and End are all
          withheld until then, since the charger itself mostly refuses those
          commands during a fault.
        </p>
      )}

      <Panel className="grid grid-cols-2 gap-4 px-4 py-4 sm:grid-cols-3 lg:grid-cols-6">
        <Readout
          label="Delivered"
          value={kwh(session.energy_wh)}
          unit="kWh"
          signal={session.state === "ACTIVE" ? "live" : undefined}
        />
        {/* Two different measurements, and the gap between them is the point:
            a car left overnight shows hours plugged in and minutes charging. */}
        <Readout
          label="Charging time"
          value={duration(session.active_seconds_live ?? session.active_seconds)}
          signal={session.state === "ACTIVE" ? "live" : undefined}
        />
        <Readout label="Plugged in for" value={duration(elapsedSeconds)} />
        <Readout label="Battery" value={pct(latestSoc, 1)} />
        <Readout label="Vehicle" value={session.vehicle_name ?? "—"} />
        <Readout label="Card" value={session.id_tag ?? "—"} />
      </Panel>

      <Panel className="grid grid-cols-1 gap-4 px-4 py-3 sm:grid-cols-3">
        <Readout label="Plugged in" value={datetime(session.plugged_in_at)} />
        <Readout
          label="Charging started"
          value={session.started_at ? datetime(session.started_at) : "Not started"}
        />
        <Readout
          label="Ended"
          value={session.ended_at ? datetime(session.ended_at) : "Still connected"}
        />
      </Panel>

      {/* Charts */}
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel>
          <PanelHeader eyebrow="Cumulative" title="Energy delivered" />
          <div className="px-2 py-3">
            <EnergyChart series={energySeries} />
          </div>
        </Panel>
        <Panel>
          <PanelHeader eyebrow="Instantaneous" title="Power draw" />
          <div className="px-2 py-3">
            <PowerChart series={powerSeries} />
          </div>
        </Panel>
        <Panel className="xl:col-span-2">
          <PanelHeader eyebrow="Vehicle" title="State of charge" />
          <div className="px-2 py-3">
            <SocChart series={socSeries} />
          </div>
        </Panel>
      </div>

      {/* Transactions */}
      <Panel>
        <PanelHeader eyebrow="OCPP" title="Transactions" />
        <Table>
          <thead>
            <tr>
              <Th>Transaction</Th>
              <Th>State</Th>
              <Th className="text-right">Meter start</Th>
              <Th className="text-right">Meter now</Th>
              <Th className="text-right">Delivered</Th>
              <Th>Started</Th>
              <Th>Reason</Th>
            </tr>
          </thead>
          <tbody>
            {(session.transactions ?? []).map((t) => {
              const last = t.meter_stop_wh ?? t.meter_last_wh ?? t.meter_start_wh;
              return (
                <tr key={t.id}>
                  <Td className="tnum text-ink">{t.ocpp_transaction_id}</Td>
                  <Td>
                    <Chip signal={t.state === "ACTIVE" ? "live" : "idle"}>{t.state}</Chip>
                  </Td>
                  <Td className="tnum text-right">{t.meter_start_wh} Wh</Td>
                  <Td className="tnum text-right">{last} Wh</Td>
                  <Td className="tnum text-right text-ink">
                    {kwh(last - t.meter_start_wh)} kWh
                  </Td>
                  <Td className="tnum">{datetime(t.started_at)}</Td>
                  <Td>{t.stop_reason ?? "—"}</Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </Panel>

      <FaultsTable sessionId={sessionId} />

      {/* Frames */}
      <Panel>
        <PanelHeader
          eyebrow="Wire"
          title="Messages exchanged during this session"
          right={
            session.messages?.length ? (
              <button
                onClick={() =>
                  setExpandedFrames(
                    expandedFrames.size
                      ? new Set()
                      : new Set((session.messages ?? []).map((m) => m.id)),
                  )
                }
                className="text-xs text-ink-faint hover:text-ink"
              >
                {expandedFrames.size ? "Collapse all" : "Expand all"}
              </button>
            ) : undefined
          }
        />
        {!session.messages?.length ? (
          <EmptyState title="No frames recorded against this session yet" />
        ) : (
          <ol className="max-h-[32rem] overflow-y-auto">
            {session.messages.map((m) => (
              <LogRow
                key={m.id}
                row={m}
                open={expandedFrames.has(m.id)}
                onToggle={() => toggleFrame(m.id)}
              />
            ))}
          </ol>
        )}
      </Panel>
    </div>
  );
}