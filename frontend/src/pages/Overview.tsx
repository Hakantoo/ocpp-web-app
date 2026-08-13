/** The landing page: every charger in the network, at a glance, grouped so
 *  a fleet of any size stays scannable. Collapsed by default -- a card shows
 *  only its name, online/offline, and connector count until you open it,
 *  which is what keeps this usable whether there are five chargers or
 *  fifty thousand. An operator should never need a second click to stop
 *  something once a card is open. */

import { useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Folder, FolderMinus, FolderPlus, Pencil, Trash2 } from "lucide-react";

import {
  useEndSession,
  useOverview,
  useSession,
  useStartCharging,
  useStopCharging,
} from "../lib/api";
import { kwh } from "../lib/format";
import { sortByFavorite, useFavorites } from "../lib/favorites";
import { matchesContainerSearch, useContainers, type Container } from "../lib/containers";
import type { ConnectorOverview } from "../lib/types";
import { ConnectorPanel } from "../components/ConnectorPanel";
import {
  DailyEnergyChart,
  FleetPanel,
  HourlyEnergyChart,
} from "../components/charts";
import {
  Button,
  ChargerSearch,
  Chip,
  ContainerPickerPortal,
  EmptyState,
  ErrorNote,
  FavoriteStar,
  Panel,
  PanelHeader,
  Readout,
  Skeleton,
} from "../components/ui";

interface ChargerGroup {
  identity: string;
  label: string | null;
  online: boolean;
  connectors: ConnectorOverview[];
}

function groupByCharger(connectors: ConnectorOverview[]): ChargerGroup[] {
  const groups = new Map<string, ChargerGroup>();
  for (const c of connectors) {
    const existing = groups.get(c.charge_point_id);
    if (existing) {
      existing.connectors.push(c);
    } else {
      groups.set(c.charge_point_id, {
        identity: c.charge_point_id,
        label: c.charge_point_label,
        online: Boolean(c.is_online),
        connectors: [c],
      });
    }
  }
  return [...groups.values()];
}

export function Overview() {
  const { data, isLoading, error } = useOverview();
  const [busy, setBusy] = useState<{ id: number; action: string } | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
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

  const groups = useMemo(
    () => (data ? groupByCharger(data.connectors) : []),
    [data],
  );

  const onlineCount = groups.filter((g) => g.online).length;
  const offlineCount = groups.length - onlineCount;

  const visibleGroups = useMemo(() => {
    let list = groups;
    if (search.trim()) {
      list = list.filter((g) =>
        matchesContainerSearch(g.identity, g.label, search, containerOf(g.identity)),
      );
    }
    if (statusFilter) {
      list = list.filter((g) => (statusFilter === "online" ? g.online : !g.online));
    }
    return sortByFavorite(list, (g) => g.identity, isFavorite);
  }, [groups, search, statusFilter, isFavorite, containerOf]);

  /** Splits the visible list into container buckets plus standalone
   *  chargers -- a charger inside a container that matched search shows
   *  its whole container, even if only this one charger matched. */
  const { containerBuckets, standalone } = useMemo(() => {
    const buckets = new Map<string, { container: Container; chargers: ChargerGroup[] }>();
    const loose: ChargerGroup[] = [];
    for (const g of visibleGroups) {
      const c = containerOf(g.identity);
      if (c) {
        const existing = buckets.get(c.id);
        if (existing) existing.chargers.push(g);
        else buckets.set(c.id, { container: c, chargers: [g] });
      } else {
        loose.push(g);
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
  }, [visibleGroups, containerOf, isFavorite]);

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
    for (const group of standalone) keys.add(group.identity);
    setCollapsed(keys);
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

      {/* Two horizons: when load lands within a day, and the day-to-day trend.
          Kept above the charger list, which can run long, so these are
          never something you have to scroll past a whole fleet to reach. */}
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

      {data.connectors.length > 0 && (
        <FleetPanel
          online={onlineCount}
          offline={offlineCount}
          activeFilter={statusFilter}
          onFilterChange={setStatusFilter}
        />
      )}

      {data.connectors.length > 0 && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex-1">
              <ChargerSearch value={search} onChange={setSearch} />
            </div>
            <Button onClick={expandAll}>Expand all</Button>
            <Button onClick={collapseAll}>Collapse all</Button>
          </div>

          {visibleGroups.length === 0 ? (
            <Panel>
              <EmptyState
                title="No chargers match"
                hint="Try a different search, or clear the online/offline filter."
              />
            </Panel>
          ) : (
            <div className="space-y-3">
              {containerBuckets.map(({ container, chargers }) => (
                <ContainerCard
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
                    busy={busy}
                    onCommand={run}
                    onRename={(name) => renameContainer(container.id, name)}
                    onDelete={() => deleteContainer(container.id)}
                    onRemoveCharger={removeFromContainer}
                  />
                ))}
                {standalone.map((group) => (
                  <ChargerGroupCard
                    key={group.identity}
                    group={group}
                    isOpen={!collapsed.has(group.identity)}
                    onToggle={() => toggleCollapsed(group.identity)}
                    isFavorite={isFavorite(group.identity)}
                    onToggleFavorite={() => toggleFavorite(group.identity)}
                    busy={busy}
                    onCommand={run}
                    containers={containers}
                    onAddToContainer={(id) => addToContainer(id, group.identity)}
                    onCreateContainer={(name) => createContainer(name, group.identity) !== null}
                  />
                ))}
              </div>
            )}
          </div>
      )}

      {data.connectors.length === 0 && (
        <Panel>
          <EmptyState
            title="No connectors yet"
            hint="Start the simulator, or point a charger at ws://localhost:9000/ocpp/{id}. It will appear here as soon as it connects."
          />
        </Panel>
      )}

    </div>
  );
}

/** One charger, collapsed to a single row by default.
 *
 *  Collapsed shows nothing about any individual connector -- just the
 *  charger's own name, its online/offline chip, and how many connectors it
 *  has. That is deliberate: with a fleet in the thousands, rendering full
 *  connector detail for every card whether or not anyone is looking at it
 *  is the difference between a page that scrolls and one that does not
 *  load. Expanding is what reveals the real ConnectorPanel cards, wired to
 *  exactly the same controls the old layout always had.
 */
function ChargerGroupCard({
  group,
  isOpen,
  onToggle,
  isFavorite,
  onToggleFavorite,
  busy,
  onCommand,
  containers,
  onAddToContainer,
  onCreateContainer,
  onRemoveFromContainer,
}: {
  group: ChargerGroup;
  isOpen: boolean;
  onToggle: () => void;
  isFavorite: boolean;
  onToggleFavorite: () => void;
  busy: { id: number; action: string } | null;
  onCommand: (connector: ConnectorOverview, action: "start" | "stop" | "end") => void;
  containers?: Container[];
  onAddToContainer?: (containerId: string) => void;
  onCreateContainer?: (name: string) => boolean;
  onRemoveFromContainer?: () => void;
}) {
  const chargingCount = group.connectors.filter(
    (c) => c.session_state === "ACTIVE",
  ).length;
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  return (
    <Panel className="overflow-hidden">
      <div className="flex w-full items-center gap-3 px-4 py-3 hover:bg-panel-high/40">
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
            active={isFavorite}
            onToggle={onToggleFavorite}
            label={group.label ?? group.identity}
          />
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold text-ink">
              {group.label ?? group.identity}
            </p>
            <p className="truncate text-xs text-ink-faint">
              {group.connectors.length} connector
              {group.connectors.length === 1 ? "" : "s"}
              {chargingCount > 0 && ` · ${chargingCount} charging`}
            </p>
          </div>
        </button>
        <Chip signal={group.online ? "live" : "idle"} pip>
          {group.online ? "Online" : "Offline"}
        </Chip>
        {onRemoveFromContainer && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onRemoveFromContainer();
            }}
            className="rounded-md p-1 text-ink-faint hover:text-signal-fault"
            aria-label={`Remove ${group.identity} from its container`}
            title="Remove from this container"
          >
            <FolderMinus size={14} />
          </button>
        )}
        {containers && (
          <div className="relative">
            <button
              ref={menuButtonRef}
              type="button"
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
                  const ok = onCreateContainer?.(name) ?? false;
                  if (ok) setMenuOpen(false);
                  return ok;
                }}
                onClose={() => setMenuOpen(false)}
              />
            )}
          </div>
        )}
      </div>

      {isOpen && (
        <div className="grid gap-4 border-t border-line p-4 sm:grid-cols-2 xl:grid-cols-3">
          {group.connectors.map((connector) => (
            <ConnectorCard
              key={connector.connector_pk}
              connector={connector}
              busy={busy?.id === connector.connector_pk ? busy.action : null}
              onCommand={(action) => onCommand(connector, action)}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}

/** A container: a named group of chargers, shown as one collapsible card
 *  containing its own member cards -- the same collapsed-by-default
 *  principle as everything else here, so a container with many chargers
 *  does not cost anything until it is opened. */
function ContainerCard({
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
  busy,
  onCommand,
  onRename,
  onDelete,
  onRemoveCharger,
}: {
  container: Container;
  chargers: ChargerGroup[];
  isOpen: boolean;
  onToggle: () => void;
  collapsedChargers: Set<string>;
  onToggleCharger: (identity: string) => void;
  isFavorite: (identity: string) => boolean;
  onToggleFavorite: (identity: string) => void;
  isContainerFavorite: boolean;
  onToggleContainerFavorite: () => void;
  busy: { id: number; action: string } | null;
  onCommand: (connector: ConnectorOverview, action: "start" | "stop" | "end") => void;
  onRename: (name: string) => void;
  onDelete: () => void;
  onRemoveCharger: (identity: string) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(container.name);
  const onlineCount = chargers.filter((c) => c.online).length;

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
        <div className="space-y-2 border-t border-line p-3">
          {chargers.map((group) => (
            <ChargerGroupCard
              key={group.identity}
              group={group}
              isOpen={!collapsedChargers.has(group.identity)}
              onToggle={() => onToggleCharger(group.identity)}
              isFavorite={isFavorite(group.identity)}
              onToggleFavorite={() => onToggleFavorite(group.identity)}
              busy={busy}
              onCommand={onCommand}
              onRemoveFromContainer={() => onRemoveCharger(group.identity)}
            />
          ))}
        </div>
      )}
    </Panel>
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