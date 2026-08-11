/** The landing page: every connector in the network, at a glance, with the
 *  controls right there. An operator should never need a second click to stop
 *  something. */

import { useState } from "react";

import {
  useEndSession,
  useOverview,
  useSession,
  useStartCharging,
  useStopCharging,
} from "../lib/api";
import { kwh } from "../lib/format";
import type { ConnectorOverview } from "../lib/types";
import { ConnectorPanel } from "../components/ConnectorPanel";
import { DailyEnergyChart, HourlyEnergyChart } from "../components/charts";
import {
  EmptyState,
  ErrorNote,
  Panel,
  PanelHeader,
  Readout,
  Skeleton,
} from "../components/ui";

export function Overview() {
  const { data, isLoading, error } = useOverview();
  const [busy, setBusy] = useState<{ id: number; action: string } | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const start = useStartCharging();
  const stop = useStopCharging();
  const end = useEndSession();

  /** Start doubles as Resume: the backend routes a Start on a held session to
   *  ClearChargingProfile, so the button means the same thing either way. */
  async function run(
    connector: ConnectorOverview,
    action: "start" | "stop" | "end",
  ) {
    setFailure(null);
    setBusy({ id: connector.connector_pk, action });
    try {
      if (action === "start") {
        await start.mutateAsync({
          identity: connector.charge_point_id,
          connector_id: connector.connector_id,
        });
      } else if (connector.session_id) {
        const mutation = action === "stop" ? stop : end;
        await mutation.mutateAsync(connector.session_id);
      }
    } catch (err) {
      setFailure(err instanceof Error ? err.message : "Command failed");
    } finally {
      setBusy(null);
    }
  }

  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-72" />
        ))}
      </div>
    );
  }

  if (error) return <ErrorNote message={(error as Error).message} />;
  if (!data) return null;

  const charging = data.connectors.filter((c) => c.session_state === "ACTIVE");
  const held = data.connectors.filter((c) => c.session_state === "PAUSED");
  const liveEnergy = data.connectors.reduce(
    (total, c) => total + (c.session_energy_wh ?? 0),
    0,
  );
  const todayKwh = data.energy_by_day[0]?.kwh ?? 0;

  return (
    <div className="space-y-5">
      <div>
        <p className="eyebrow mb-1.5">Network</p>
        <h1 className="text-2xl font-semibold tracking-tight">Live status</h1>
      </div>

      {failure && <ErrorNote message={failure} />}

      {/* Instrument strip */}
      <Panel className="grid grid-cols-2 gap-4 px-4 py-4 sm:grid-cols-4">
        <Readout
          label="Delivering"
          value={charging.length}
          unit={`of ${data.connectors.length}`}
          signal={charging.length ? "live" : undefined}
        />
        <Readout
          label="Held"
          value={held.length}
          signal={held.length ? "hold" : undefined}
        />
        <Readout label="In progress" value={kwh(liveEnergy)} unit="kWh" />
        <Readout label="Today" value={todayKwh.toFixed(1)} unit="kWh" />
      </Panel>

      {/* Connectors */}
      {data.connectors.length === 0 ? (
        <Panel>
          <EmptyState
            title="No connectors yet"
            hint="Start the simulator, or point a charger at ws://localhost:9000/ocpp/{id}. It will appear here as soon as it connects."
          />
        </Panel>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {data.connectors.map((connector) => (
            <ConnectorCard
              key={connector.connector_pk}
              connector={connector}
              busy={busy?.id === connector.connector_pk ? busy.action : null}
              onCommand={(action) => run(connector, action)}
            />
          ))}
        </div>
      )}

      {/* Two horizons: when load lands within a day, and the day-to-day trend. */}
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel>
          <PanelHeader
            eyebrow="Last 48 hours"
            title="Energy delivered per hour"
            right={
              <span className="text-xs text-ink-faint">from meter readings</span>
            }
          />
          <div className="px-2 py-4">
            <HourlyEnergyChart data={data.energy_by_hour} />
          </div>
        </Panel>

        <Panel>
          <PanelHeader eyebrow="Last 14 days" title="Energy delivered per day" />
          <div className="px-2 py-4">
            <DailyEnergyChart data={data.energy_by_day} />
          </div>
        </Panel>
      </div>
    </div>
  );
}

/** Wraps a panel so each card can pull its own meter trace without the parent
 *  refetching every session on every tick. */
function ConnectorCard({
  connector,
  busy,
  onCommand,
}: {
  connector: ConnectorOverview;
  busy: string | null;
  onCommand: (action: "start" | "stop" | "end") => void;
}) {
  const { data: session } = useSession(connector.session_id);
  const trace = session?.series?.["Energy.Active.Import.Register"] ?? [];

  return (
    <ConnectorPanel
      connector={connector}
      trace={trace}
      busy={busy}
      onStart={() => onCommand("start")}
      onStop={() => onCommand("stop")}
      onEnd={() => onCommand("end")}
    />
  );
}