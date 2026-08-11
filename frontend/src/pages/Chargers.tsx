/** Hardware inventory: every charger the CSMS knows about, real or
 *  simulated, with its uptime and connector states at a glance.
 *
 *  This page only manages what already exists -- editing settings, deleting
 *  a record. Creating new hardware happens elsewhere: a real charger
 *  provisions itself the moment it connects, and a fake one is provisioned
 *  from the Simulator page, which is the one place that actually mimics
 *  installing new hardware. */

import { useState } from "react";
import { Link } from "react-router-dom";
import { Cpu, Pencil, Trash2 } from "lucide-react";

import {
  useChargePoints,
  useDeleteChargePoint,
  useUptimeSummary,
} from "../lib/api";
import type { ChargePoint } from "../lib/types";
import { duration, since } from "../lib/format";
import { connectorSignal } from "../lib/status";
import {
  Button,
  Chip,
  EmptyState,
  ErrorNote,
  Modal,
  Note,
  Panel,
  Skeleton,
  Table,
  Td,
  Th,
  cx,
} from "../components/ui";

export function Chargers() {
  const { data, isLoading, error } = useChargePoints();
  const [editing, setEditing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);

  if (isLoading) return <Skeleton className="h-64" />;
  if (error) return <ErrorNote message={(error as Error).message} />;

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="eyebrow mb-1.5">Hardware</p>
          <h1 className="text-2xl font-semibold tracking-tight">Chargers</h1>
        </div>
        {Boolean(data?.length) && (
          <Button onClick={() => setEditing((e) => !e)}>
            <Pencil size={13} /> {editing ? "Done" : "Edit"}
          </Button>
        )}
      </div>

      {!data?.length ? (
        <Panel>
          <EmptyState
            title="No chargers registered"
            hint="A real charger registers itself the first time it connects to ws://localhost:9000/ocpp/{id}. To create a fake one, provision it on the Simulator page."
          />
        </Panel>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {data.map((cp) => (
            <ChargerCard
              key={cp.identity}
              cp={cp}
              editing={editing}
              onRequestDelete={() => setConfirmingDelete(cp.identity)}
            />
          ))}
        </div>
      )}

      {confirmingDelete && (
        <DeleteConfirm
          identity={confirmingDelete}
          onClose={() => setConfirmingDelete(null)}
        />
      )}
    </div>
  );
}

function ChargerCard({
  cp,
  editing,
  onRequestDelete,
}: {
  cp: ChargePoint;
  editing: boolean;
  onRequestDelete: () => void;
}) {
  const { data: uptime } = useUptimeSummary(cp.identity);
  const streak = uptime?.streak;

  return (
    <Panel className="animate-rise">
      <div className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <span
            className={cx(
              "grid h-9 w-9 shrink-0 place-items-center rounded-lg border",
              cp.live
                ? "border-signal-live/40 bg-signal-live/10 text-signal-live"
                : "border-line bg-panel-high text-ink-faint",
            )}
          >
            <Cpu size={16} />
          </span>
          <div className="min-w-0">
            <Link
              to={`/chargers/${cp.identity}`}
              className="block truncate text-sm font-semibold hover:text-signal-live"
            >
              {cp.label ?? cp.identity}
            </Link>
            <p className="tnum truncate text-xs text-ink-faint">
              {cp.identity} · {cp.vendor ?? "unknown"} {cp.model ?? ""}
            </p>
          </div>
        </div>
        <div className="shrink-0 text-right">
          {editing ? (
            cp.is_simulated ? (
              <Link
                to="/simulator"
                className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-xs text-ink-faint hover:text-ink"
              >
                Simulated — manage on the Simulator page
              </Link>
            ) : (
              <Button variant="danger" onClick={onRequestDelete}>
                <Trash2 size={13} /> Delete
              </Button>
            )
          ) : (
            <>
              <Chip signal={cp.live ? "live" : "idle"} pip={Boolean(cp.live)}>
                {cp.live ? "Online" : "Offline"}
              </Chip>
              {/* One honest line under the chip: how long it has been in this
                  state. Nothing shown at all if there is no history yet, rather
                  than a fabricated duration. */}
              {streak?.seconds != null && (
                <p className="tnum mt-1 text-xs text-ink-faint">
                  {streak.connected
                    ? `Up for ${duration(streak.seconds)}`
                    : `Last seen ${duration(streak.seconds)} ago`}
                </p>
              )}
            </>
          )}
        </div>
      </div>

      <Table>
        <thead>
          <tr>
            <Th>Connector</Th>
            <Th>State</Th>
            <Th className="text-right">Updated</Th>
          </tr>
        </thead>
        <tbody>
          {(cp.connectors ?? [])
            .filter((c) => c.connector_id > 0)
            .map((c) => (
              <tr key={c.id}>
                <Td className="tnum text-ink">#{c.connector_id}</Td>
                <Td>
                  <Chip signal={connectorSignal(c.status)}>{c.status}</Chip>
                </Td>
                <Td className="tnum text-right">{since(c.status_updated_at)} ago</Td>
              </tr>
            ))}
        </tbody>
      </Table>

      <p className="px-4 py-2.5 text-xs text-ink-faint">
        Firmware {cp.firmware_version ?? "—"} · heartbeat every{" "}
        {cp.heartbeat_interval}s · last seen {since(cp.last_seen)} ago
      </p>
    </Panel>
  );
}

/** Two clicks to delete, on purpose: Delete above opens this, and this asks
 *  again before anything happens -- removing a charger takes its whole
 *  history with it, cascaded, and that is not something a single misclick
 *  should be able to do. */
function DeleteConfirm({ identity, onClose }: { identity: string; onClose: () => void }) {
  const del = useDeleteChargePoint();
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setError(null);
    try {
      await del.mutateAsync(identity);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete that charger");
    }
  }

  return (
    <Modal
      title={`Delete ${identity}?`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" busy={del.isPending} onClick={confirm}>
            <Trash2 size={13} /> Yes, delete it
          </Button>
        </>
      }
    >
      {error && <ErrorNote message={error} />}
      <Note tone="error">
        This removes {identity} and its connectors. Sessions, faults, and
        uptime history are kept, not destroyed. Refused if a session is still
        open.
      </Note>
    </Modal>
  );
}