/** Hardware inventory: every charger the CSMS knows about, real or
 *  simulated, with its uptime and connector states at a glance.
 *
 *  This page only manages what already exists -- editing settings, deleting
 *  a record. Creating new hardware happens elsewhere: a real charger
 *  provisions itself the moment it connects, and a fake one is provisioned
 *  from the Simulator page, which is the one place that actually mimics
 *  installing new hardware. */

import { useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, Cpu, Folder, FolderMinus, FolderPlus, Pencil, Trash2 } from "lucide-react";

import {
  useChargePoints,
  useDeleteChargePoint,
  useUptimeSummary,
} from "../lib/api";
import type { ChargePoint } from "../lib/types";
import { duration, since } from "../lib/format";
import { connectorSignal } from "../lib/status";
import { sortByFavorite, useFavorites } from "../lib/favorites";
import { matchesContainerSearch, useContainers, type Container } from "../lib/containers";
import { FleetPanel } from "../components/charts";
import {
  Button,
  ChargerSearch,
  Chip,
  ContainerPickerPortal,
  EmptyState,
  ErrorNote,
  FavoriteStar,
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
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"online" | "offline" | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const { isFavorite, toggleFavorite } = useFavorites();
  const {
    containers,
    containerOf,
    createContainer,
    renameContainer,
    deleteContainer,
    addToContainer,
    removeFromContainer,
  } = useContainers();

  const visible = useMemo(() => {
    if (!data) return [];
    let filtered = search.trim()
      ? data.filter((cp) =>
          matchesContainerSearch(cp.identity, cp.label, search, containerOf(cp.identity)),
        )
      : data;
    if (statusFilter) {
      filtered = filtered.filter((cp) =>
        statusFilter === "online" ? Boolean(cp.is_online) : !cp.is_online,
      );
    }
    return sortByFavorite(filtered, (cp) => cp.identity, isFavorite);
  }, [data, search, statusFilter, isFavorite, containerOf]);

  const { containerBuckets, standalone } = useMemo(() => {
    const buckets = new Map<string, { container: Container; chargers: ChargePoint[] }>();
    const loose: ChargePoint[] = [];
    for (const cp of visible) {
      const container = containerOf(cp.identity);
      if (container) {
        const existing = buckets.get(container.id);
        if (existing) existing.chargers.push(cp);
        else buckets.set(container.id, { container, chargers: [cp] });
      } else {
        loose.push(cp);
      }
    }
    return {
      containerBuckets: sortByFavorite(
        [...buckets.values()],
        (b) => b.container.id,
        isFavorite,
      ),
      standalone: loose,
    };
  }, [visible, containerOf, isFavorite]);

  function toggleCollapsed(identity: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(identity)) next.delete(identity);
      else next.add(identity);
      return next;
    });
  }

  function expandAll() {
    setCollapsed(new Set());
  }

  function collapseAll() {
    const keys = new Set<string>();
    for (const { container } of containerBuckets) keys.add(`container:${container.id}`);
    for (const cp of standalone) keys.add(cp.identity);
    setCollapsed(keys);
  }

  const onlineCount = data?.filter((cp) => cp.is_online).length ?? 0;
  const offlineCount = (data?.length ?? 0) - onlineCount;

  if (isLoading) return <Skeleton className="h-64" />;
  if (error) return <ErrorNote message={(error as Error).message} />;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
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

      {Boolean(data?.length) && (
        <FleetPanel
          online={onlineCount}
          offline={offlineCount}
          activeFilter={statusFilter}
          onFilterChange={setStatusFilter}
        />
      )}

      {!data?.length ? (
        <Panel>
          <EmptyState
            title="No chargers registered"
            hint="A real charger registers itself the first time it connects to ws://localhost:9000/ocpp/{id}. To create a fake one, provision it on the Simulator page."
          />
        </Panel>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            {data.length > 3 && (
              <div className="flex-1">
                <ChargerSearch value={search} onChange={setSearch} />
              </div>
            )}
            <Button onClick={expandAll}>Expand all</Button>
            <Button onClick={collapseAll}>Collapse all</Button>
          </div>

          {visible.length === 0 ? (
            <Panel>
              <EmptyState title="No chargers match" hint="Try a different search." />
            </Panel>
          ) : (
            <div className="space-y-3">
              {containerBuckets.map(({ container, chargers }) => (
                <ChargerContainerCard
                  key={container.id}
                  container={container}
                  chargers={chargers}
                  isOpen={!collapsed.has(`container:${container.id}`)}
                  onToggle={() => toggleCollapsed(`container:${container.id}`)}
                  collapsedChargers={collapsed}
                  onToggleCharger={toggleCollapsed}
                  isFavorite={isFavorite}
                  onToggleFavorite={toggleFavorite}
                  isContainerFavorite={isFavorite(container.id)}
                  onToggleContainerFavorite={() => toggleFavorite(container.id)}
                  onRename={(name) => renameContainer(container.id, name)}
                  onDelete={() => deleteContainer(container.id)}
                  onRemoveCharger={removeFromContainer}
                  editing={editing}
                  onRequestDelete={setConfirmingDelete}
                />
              ))}
              <div className="grid gap-4 md:grid-cols-2">
                {standalone.map((cp) => (
                  <ChargerCard
                    key={cp.identity}
                    cp={cp}
                    editing={editing}
                    onRequestDelete={() => setConfirmingDelete(cp.identity)}
                    isFavorite={isFavorite(cp.identity)}
                    onToggleFavorite={() => toggleFavorite(cp.identity)}
                    isOpen={!collapsed.has(cp.identity)}
                    onToggle={() => toggleCollapsed(cp.identity)}
                    containers={containers}
                    onAddToContainer={(id) => addToContainer(id, cp.identity)}
                    onCreateContainer={(name) => createContainer(name, cp.identity)}
                  />
                ))}
              </div>
            </div>
          )}
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

/** A container of real/simulated hardware, shown as one collapsible card
 *  with each member's own full ChargerCard nested inside when open --
 *  the same pattern as Overview's ContainerCard. */
function ChargerContainerCard({
  container,
  chargers,
  isOpen,
  onToggle,
  collapsedChargers,
  onToggleCharger,
  isFavorite,
  onToggleFavorite,
  isContainerFavorite,
  onToggleContainerFavorite,
  onRename,
  onDelete,
  onRemoveCharger,
  editing,
  onRequestDelete,
}: {
  container: Container;
  chargers: ChargePoint[];
  isOpen: boolean;
  onToggle: () => void;
  collapsedChargers: Set<string>;
  onToggleCharger: (identity: string) => void;
  isFavorite: (identity: string) => boolean;
  onToggleFavorite: (identity: string) => void;
  isContainerFavorite: boolean;
  onToggleContainerFavorite: () => void;
  onRename: (name: string) => void;
  onDelete: () => void;
  onRemoveCharger: (identity: string) => void;
  editing: boolean;
  onRequestDelete: (identity: string) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(container.name);
  const onlineCount = chargers.filter((cp) => cp.is_online).length;

  return (
    <Panel className="overflow-hidden border-signal-wait/25">
      <div className="flex w-full items-center gap-3 bg-panel-high/30 px-4 py-3">
        <button
          type="button"
          onClick={onToggle}
          className="flex flex-1 items-center gap-3 text-left"
        >
          {isOpen ? (
            <ChevronDown size={14} className="shrink-0 text-ink-faint" />
          ) : (
            <ChevronRight size={14} className="shrink-0 text-ink-faint" />
          )}
          <FavoriteStar
            active={isContainerFavorite}
            onToggle={onToggleContainerFavorite}
            label={container.name}
          />
          <Folder size={14} className="shrink-0 text-signal-wait" />
          {renaming ? (
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => {
                if (e.key === "Enter" && name.trim()) {
                  onRename(name.trim());
                  setRenaming(false);
                }
                if (e.key === "Escape") setRenaming(false);
              }}
              onBlur={() => setRenaming(false)}
              className="min-w-0 flex-1 rounded-md border border-line bg-panel px-1.5 py-0.5 text-sm text-ink focus:outline-none"
            />
          ) : (
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-semibold text-ink">{container.name}</p>
              <p className="truncate text-xs text-ink-faint">
                {chargers.length} charger{chargers.length === 1 ? "" : "s"} ·{" "}
                {onlineCount} online
              </p>
            </div>
          )}
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setRenaming(true);
          }}
          className="rounded-md p-1 text-ink-faint hover:text-ink"
          aria-label="Rename container"
        >
          <Pencil size={13} />
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          className="rounded-md p-1 text-ink-faint hover:text-signal-fault"
          aria-label="Delete container"
          title="Ungroups these chargers -- does not delete them"
        >
          <Trash2 size={13} />
        </button>
      </div>

      {isOpen && (
        <div className="grid gap-4 border-t border-line p-3 md:grid-cols-2">
          {chargers.map((cp) => (
            <ChargerCard
              key={cp.identity}
              cp={cp}
              editing={editing}
              onRequestDelete={() => onRequestDelete(cp.identity)}
              isFavorite={isFavorite(cp.identity)}
              onToggleFavorite={() => onToggleFavorite(cp.identity)}
              isOpen={!collapsedChargers.has(cp.identity)}
              onToggle={() => onToggleCharger(cp.identity)}
              onRemoveFromContainer={() => onRemoveCharger(cp.identity)}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}

function ChargerCard({
  cp,
  editing,
  onRequestDelete,
  isFavorite,
  onToggleFavorite,
  isOpen,
  onToggle,
  containers,
  onAddToContainer,
  onCreateContainer,
  onRemoveFromContainer,
}: {
  cp: ChargePoint;
  editing: boolean;
  onRequestDelete: () => void;
  isFavorite: boolean;
  onToggleFavorite: () => void;
  isOpen?: boolean;
  onToggle?: () => void;
  containers?: Container[];
  onAddToContainer?: (containerId: string) => void;
  onCreateContainer?: (name: string) => void;
  onRemoveFromContainer?: () => void;
}) {
  const { data: uptime } = useUptimeSummary(cp.identity);
  const streak = uptime?.streak;
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  return (
    <Panel className="animate-rise overflow-hidden">
      <div className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
        <button
          type="button"
          onClick={onToggle}
          disabled={!onToggle}
          className="flex min-w-0 flex-1 items-center gap-3 text-left"
        >
          {onToggle &&
            (isOpen ? (
              <ChevronDown size={14} className="shrink-0 text-ink-faint" />
            ) : (
              <ChevronRight size={14} className="shrink-0 text-ink-faint" />
            ))}
          <FavoriteStar
            active={isFavorite}
            onToggle={onToggleFavorite}
            label={cp.label ?? cp.identity}
          />
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
              onClick={(e) => e.stopPropagation()}
              className="block truncate text-sm font-semibold hover:text-signal-live"
            >
              {cp.label ?? cp.identity}
            </Link>
            <p className="tnum truncate text-xs text-ink-faint">
              {cp.identity} · {cp.vendor ?? "unknown"} {cp.model ?? ""}
            </p>
          </div>
        </button>
        <div className="flex shrink-0 items-center gap-2">
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
            <div className="text-right">
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
            </div>
          )}
          {containers && (
            <div className="relative">
              <button
                type="button"
                ref={menuButtonRef}
                onClick={(e) => {
                  e.stopPropagation();
                  setMenuOpen((v) => !v);
                }}
                className="rounded-md p-1 text-ink-faint hover:text-ink"
                aria-label="Add to container"
              >
                <FolderPlus size={14} />
              </button>
              {menuOpen && menuButtonRef.current && (
                <ContainerPickerPortal
                  anchor={menuButtonRef.current}
                  containers={containers}
                  onPick={(id) => {
                    onAddToContainer?.(id);
                    setMenuOpen(false);
                  }}
                  onCreate={(name) => {
                    onCreateContainer?.(name);
                    setMenuOpen(false);
                  }}
                  onClose={() => setMenuOpen(false)}
                />
              )}
            </div>
          )}
          {onRemoveFromContainer && (
            <button
              type="button"
              onClick={() => onRemoveFromContainer()}
              className="rounded-md p-1 text-ink-faint hover:text-signal-fault"
              aria-label={`Remove ${cp.identity} from its container`}
              title="Remove from this container"
            >
              <FolderMinus size={14} />
            </button>
          )}
        </div>
      </div>

      {(isOpen ?? true) && (
        <>
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
        </>
      )}
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