/** Session history.
 *
 *  Rows are clickable in full rather than only on the ID, which is the
 *  smallest possible target and the one people aim at least.
 */

import { useNavigate } from "react-router-dom";

import { useSessions } from "../lib/api";
import { datetime, duration, kwh } from "../lib/format";
import { sessionSignal } from "../lib/status";
import {
  Chip,
  EmptyState,
  ErrorNote,
  Panel,
  Skeleton,
  Table,
  Td,
  Th,
} from "../components/ui";

export function Sessions() {
  const { data, isLoading, error } = useSessions({ limit: 200 });
  const navigate = useNavigate();

  if (isLoading) return <Skeleton className="h-72" />;
  if (error) return <ErrorNote message={(error as Error).message} />;

  return (
    <div className="space-y-5">
      <div>
        <p className="eyebrow mb-1.5">History</p>
        <h1 className="text-2xl font-semibold tracking-tight">Sessions</h1>
      </div>

      <Panel>
        {!data?.length ? (
          <EmptyState
            title="No sessions yet"
            hint="A session begins the moment a cable is connected, before any charging starts."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>ID</Th>
                <Th>State</Th>
                <Th>Where</Th>
                <Th>Vehicle</Th>
                <Th>Card</Th>
                <Th className="text-right">Energy</Th>
                <Th className="text-right">Charging</Th>
                <Th>Started</Th>
              </tr>
            </thead>
            <tbody>
              {data.map((s) => (
                <tr
                  key={s.id}
                  onClick={() => navigate(`/sessions/${s.id}`)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      navigate(`/sessions/${s.id}`);
                    }
                  }}
                  tabIndex={0}
                  role="link"
                  aria-label={`Session ${s.id}`}
                  className="cursor-pointer hover:bg-panel-high/60 focus:bg-panel-high/60 focus:outline-none"
                >
                  <Td className="tnum text-ink">{s.id}</Td>
                  <Td>
                    <Chip signal={sessionSignal(s.state)} pip={s.state === "ACTIVE"}>
                      {s.state}
                    </Chip>
                  </Td>
                  <Td className="tnum">
                    {s.charge_point_id}·{s.connector_id}
                  </Td>
                  {/* A card is not a vehicle. Showing the card number here when
                      no car is known made a session look like it was charging
                      something called RFID-0001. */}
                  <Td>{s.vehicle_name ?? "—"}</Td>
                  <Td className="tnum">{s.id_tag ?? "—"}</Td>
                  <Td className="tnum text-right text-ink">{kwh(s.energy_wh)} kWh</Td>
                  <Td className="tnum text-right">
                    {duration(s.active_seconds_live ?? s.active_seconds)}
                  </Td>
                  <Td className="tnum">{datetime(s.started_at ?? s.plugged_in_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>
    </div>
  );
}