/** A fault occurrence table: when, where, what, and (globally) which session.
 *
 *  One component, two call sites. The Protocol Log mounts it unfiltered (or
 *  filtered by charger, matching the log above it); a session's detail page
 *  mounts it filtered to that session's id and drops the Session column,
 *  since every row would say the same thing.
 *
 *  A fault no longer ends a session -- the charger frequently keeps a
 *  transaction running straight through one -- so this table is the only
 *  place a fault is visible at all once it clears. Ongoing faults show
 *  "ongoing" rather than a blank, since a connector currently faulted is worth
 *  noticing at a glance rather than reading as a missing value.
 */

import { Link } from "react-router-dom";

import { useFaults } from "../lib/api";
import { datetime } from "../lib/format";
import { EmptyState, ErrorNote, Panel, PanelHeader, Skeleton, Table, Td, Th } from "./ui";

export function FaultsTable({
  chargePointId,
  sessionId,
  title = "Faults",
}: {
  chargePointId?: string;
  sessionId?: number;
  title?: string;
}) {
  const { data, isLoading, error } = useFaults({
    charge_point_id: chargePointId,
    session_id: sessionId,
  });

  // On a session page, an empty table would just be noise below a session
  // that never faulted -- most sessions. Say nothing rather than show an
  // empty panel. The global Protocol Log table always renders, since an
  // explicit "nothing here" is the useful answer there.
  if (sessionId !== undefined && !isLoading && !data?.length) return null;

  const showSession = sessionId === undefined;

  return (
    <Panel className="overflow-hidden">
      <PanelHeader eyebrow="Faults" title={title} />

      {error && <ErrorNote message={(error as Error).message} />}

      {isLoading ? (
        <Skeleton className="h-40" />
      ) : !data?.length ? (
        <EmptyState
          title="No faults recorded"
          hint="A Faulted status from any charger appears here, whether or not it clears."
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Occurred</Th>
              <Th>Cleared</Th>
              <Th>Charger</Th>
              <Th>Connector</Th>
              {showSession && <Th>Session</Th>}
              <Th>Error code</Th>
              <Th>Vendor error code</Th>
            </tr>
          </thead>
          <tbody>
            {data.map((f) => (
              <tr key={f.id}>
                <Td className="tnum">{datetime(f.occurred_at)}</Td>
                <Td className="tnum">
                  {f.cleared_at ? (
                    datetime(f.cleared_at)
                  ) : (
                    <span className="text-signal-fault">ongoing</span>
                  )}
                </Td>
                <Td>{f.charge_point_label ?? f.charge_point_id}</Td>
                <Td className="tnum">{f.connector_id}</Td>
                {showSession && (
                  <Td>
                    {f.session_id ? (
                      <Link
                        to={`/sessions/${f.session_id}`}
                        className="text-signal-live hover:underline"
                      >
                        {f.session_id}
                      </Link>
                    ) : (
                      <span className="text-ink-faint">—</span>
                    )}
                  </Td>
                )}
                <Td>{f.error_code ?? <span className="text-ink-faint">—</span>}</Td>
                <Td>
                  {f.vendor_error_code ?? <span className="text-ink-faint">—</span>}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
}