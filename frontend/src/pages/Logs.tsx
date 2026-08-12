/** Every OCPP frame, both directions.
 *
 *  The first place to look when hardware misbehaves, so three things matter:
 *  rows are one uniform line so the eye can scan them, the direction marker is
 *  a fixed width so nothing wraps, and the full payload is always reachable --
 *  a truncated frame is worse than useless when you are chasing a field that
 *  arrived with the wrong value.
 */

import { useMemo, useRef, useState } from "react";
import { Check, Search, X } from "lucide-react";

import { useChargePoints, useLogs } from "../lib/api";
import { LogRow } from "../components/LogRow";
import { FaultsTable } from "../components/FaultsTable";
import { EmptyState, ErrorNote, Panel, Skeleton, cx } from "../components/ui";

const ACTIONS = [
  "BootNotification",
  "Heartbeat",
  "StatusNotification",
  "Authorize",
  "StartTransaction",
  "StopTransaction",
  "MeterValues",
  "RemoteStartTransaction",
  "RemoteStopTransaction",
  "SetChargingProfile",
  "ClearChargingProfile",
  "ChangeConfiguration",
  "GetConfiguration",
  "TriggerMessage",
  "Reset",
  "ChangeAvailability",
  "UnlockConnector",
  "ClearCache",
  "GetDiagnostics",
  "GetLocalListVersion",
  "SendLocalList",
  "ReserveNow",
  "CancelReservation",
  "GetCompositeSchedule",
  "UpdateFirmware",
  "DataTransfer",
];

const selectClass =
  "rounded-lg border border-line bg-panel px-2.5 py-1.5 text-xs text-ink-dim focus:text-ink";

/** A charger filter you can type into.
 *
 *  With a handful of chargers a dropdown is fine; with a site full of them,
 *  scrolling to find CP-0042 is not. Typing narrows the list to what matches,
 *  and the match is on substring rather than prefix so a serial fragment finds
 *  its charger too. The list is keyboard-navigable because the whole page is
 *  meant to be usable without reaching for the mouse mid-investigation.
 */
function ChargerFilter({
  chargers,
  value,
  onChange,
}: {
  chargers: string[];
  value: string;
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const boxRef = useRef<HTMLDivElement>(null);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = q
      ? chargers.filter((c) => c.toLowerCase().includes(q))
      : chargers;
    return all.slice(0, 50);
  }, [chargers, query]);

  function commit(identity: string) {
    onChange(identity);
    setOpen(false);
    setQuery("");
    setActive(0);
  }

  return (
    <div ref={boxRef} className="relative w-44">
      {/* The input is the box: same styling as the dropdowns beside it, and it
          fills the whole rectangle so a click anywhere lands the cursor. The
          search icon and clear button sit on top without stealing clicks. */}
      <Search
        size={12}
        className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint"
      />
      <input
        className={cx(
          selectClass,
          "w-full cursor-text bg-panel pl-7 pr-7 text-ink placeholder:text-ink-faint focus:outline-none focus-visible:ring-0",
        )}
        placeholder={value || "All chargers"}
        value={open ? query : value}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setActive(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((i) => Math.min(i + 1, matches.length));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((i) => Math.max(i - 1, 0));
          } else if (e.key === "Enter") {
            e.preventDefault();
            if (active === 0) commit("");
            else if (matches[active - 1]) commit(matches[active - 1]);
          } else if (e.key === "Escape") {
            setOpen(false);
            setQuery("");
          }
        }}
      />
      {value && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            commit("");
          }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-faint hover:text-ink"
          aria-label="Clear charger filter"
        >
          <X size={12} />
        </button>
      )}

      {open && (
        <>
          {/* A click anywhere else closes the list without selecting. */}
          <div
            className="fixed inset-0 z-10"
            onClick={() => {
              setOpen(false);
              setQuery("");
            }}
          />
          <ul className="absolute z-20 mt-1 max-h-64 w-52 overflow-auto rounded-lg border border-line bg-panel-high py-1 shadow-xl">
            <li>
              <button
                className={cx(
                  "flex w-full items-center px-3 py-1.5 text-left text-xs",
                  active === 0 ? "bg-panel-raised text-ink" : "text-ink-dim",
                )}
                onMouseEnter={() => setActive(0)}
                onClick={() => commit("")}
              >
                All chargers
              </button>
            </li>
            {matches.map((identity, i) => (
              <li key={identity}>
                <button
                  className={cx(
                    "tnum flex w-full items-center justify-between px-3 py-1.5 text-left text-xs",
                    active === i + 1 ? "bg-panel-raised text-ink" : "text-ink-dim",
                  )}
                  onMouseEnter={() => setActive(i + 1)}
                  onClick={() => commit(identity)}
                >
                  {identity}
                  {value === identity && <Check size={12} className="text-signal-live" />}
                </button>
              </li>
            ))}
            {!matches.length && (
              <li className="px-3 py-2 text-xs text-ink-faint">No match</li>
            )}
          </ul>
        </>
      )}
    </div>
  );
}

export function Logs() {
  const [chargePoint, setChargePoint] = useState("");
  const [action, setAction] = useState("");
  const [direction, setDirection] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const { data: chargers } = useChargePoints();
  const { data, isLoading, error } = useLogs({
    charge_point_id: chargePoint || undefined,
    action: action || undefined,
    direction: direction || undefined,
    limit: 300,
  });

  function toggle(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="eyebrow mb-1.5">Wire</p>
          <h1 className="text-2xl font-semibold tracking-tight">Protocol Logs</h1>
        </div>
        {data && data.length > 0 && (
          <button
            onClick={() =>
              setExpanded(
                expanded.size ? new Set() : new Set(data.map((r) => r.id)),
              )
            }
            className="text-xs text-ink-faint underline-offset-4 hover:text-ink hover:underline"
          >
            {expanded.size ? "Collapse all" : "Expand all"}
          </button>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <ChargerFilter
          chargers={(chargers ?? []).map((cp) => cp.identity)}
          value={chargePoint}
          onChange={setChargePoint}
        />
        <select
          className={selectClass}
          value={action}
          onChange={(e) => setAction(e.target.value)}
        >
          <option value="">All messages</option>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <select
          className={selectClass}
          value={direction}
          onChange={(e) => setDirection(e.target.value)}
        >
          <option value="">Both directions</option>
          <option value="INBOUND">From charger</option>
          <option value="OUTBOUND">To charger</option>
        </select>
      </div>

      {error && <ErrorNote message={(error as Error).message} />}

      <Panel className="overflow-hidden">
        {isLoading ? (
          <Skeleton className="h-96" />
        ) : !data?.length ? (
          <EmptyState
            title="Nothing matches those filters"
            hint="Frames appear here the moment a charger connects."
          />
        ) : (
          <ol className="max-h-[72vh] overflow-y-auto">
            {data.map((row) => (
              <LogRow
                key={row.id}
                row={row}
                open={expanded.has(row.id)}
                onToggle={() => toggle(row.id)}
              />
            ))}
          </ol>
        )}
      </Panel>

      <FaultsTable chargePointId={chargePoint || undefined} />
    </div>
  );
}