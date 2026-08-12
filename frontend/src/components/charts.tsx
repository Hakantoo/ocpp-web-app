/** Session charts.
 *
 *  Recharts, themed down to the panel vocabulary: no gridlines competing with
 *  the trace, monospace tick labels, and the same signal colours used
 *  everywhere else so a green line always means "delivering power".
 */

import { useState } from "react";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";


import { clock } from "../lib/format";
import { SIGNAL_HEX } from "../lib/status";
import { useTheme } from "../lib/theme";
import { Panel, cx } from "./ui";
import type { SeriesPoint, UptimeSegment } from "../lib/types";
import { EmptyState } from "./ui";

/** Resolve the panel's theme colours to concrete strings for Recharts, which
 *  cannot take a CSS var() reference. Re-read whenever the theme changes so a
 *  switch to light immediately darkens the axes and the hover dot's ring. */
function useChartColors() {
  // Subscribing to the theme is what forces a re-read on toggle; the value
  // itself is not used directly.
  useTheme();
  const read = (name: string) => {
    if (typeof window === "undefined") return "#3A4459";
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    // Variables are stored as "r g b"; wrap them for use as a colour.
    return v ? `rgb(${v})` : "#3A4459";
  };
  return {
    axis: read("--line-bright"),
    grid: read("--line"),
    tick: read("--ink-faint"),
    // The ring around a hovered point: white on dark, black on light. That is
    // the ink colour, which already inverts between themes -- the panel colour
    // was backwards here, dark on a dark panel.
    dotRing: read("--ink"),
  };
}

function axisProps(colors: ReturnType<typeof useChartColors>) {
  return {
    stroke: colors.axis,
    tick: { fill: colors.tick, fontSize: 10, fontFamily: "JetBrains Mono" },
    tickLine: false,
  } as const;
}

function TooltipBox({
  active,
  payload,
  label,
  unit,
  scale = 1,
  digits = 2,
}: {
  active?: boolean;
  payload?: { value: number }[];
  label?: string;
  unit: string;
  scale?: number;
  digits?: number;
}) {
  if (!active || !payload?.length) return null;
  // Hour buckets arrive as "2026-07-22T14"; anything longer is a real instant.
  const heading =
    label && label.length === 13 ? `${label.slice(11)}:00` : clock(label);
  return (
    <div className="rounded-lg border border-line bg-panel-high px-2.5 py-1.5 shadow-panel">
      <p className="eyebrow mb-0.5">{heading}</p>
      <p className="tnum text-sm text-ink">
        {(payload[0].value * scale).toFixed(digits)}
        <span className="ml-1 text-xs text-ink-faint">{unit}</span>
      </p>
    </div>
  );
}

/** Cumulative energy. Rises while charging, goes flat while held. */
export function EnergyChart({ series }: { series: SeriesPoint[] }) {
  const colors = useChartColors();
  if (series.length < 2) {
    return <EmptyState title="No energy readings yet" hint="Readings appear once a transaction is open." />;
  }
  const base = series[0].v;
  const data = series.map((p) => ({ t: p.t, v: (p.v - base) / 1000 }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
        <defs>
          <linearGradient id="energyFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SIGNAL_HEX.live} stopOpacity={0.32} />
            <stop offset="100%" stopColor={SIGNAL_HEX.live} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={colors.grid} strokeDasharray="2 4" vertical={false} />
        <XAxis dataKey="t" tickFormatter={clock} minTickGap={44} {...axisProps(colors)} />
        <YAxis width={54} {...axisProps(colors)} />
        <Tooltip content={<TooltipBox unit="kWh" />} cursor={{ stroke: colors.axis }} />
        <Area
          type="monotone"
          dataKey="v"
          stroke={SIGNAL_HEX.live}
          strokeWidth={1.75}
          fill="url(#energyFill)"
          isAnimationActive={false}
          dot={false}
          activeDot={{ strokeWidth: 2, r: 4, stroke: colors.dotRing }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** Instantaneous power. Drops to zero on a hold, and tapers above ~80% SoC. */
export function PowerChart({ series }: { series: SeriesPoint[] }) {
  const colors = useChartColors();
  if (series.length < 2) {
    return <EmptyState title="No power readings yet" />;
  }
  const data = series.map((p) => ({ t: p.t, v: p.v / 1000 }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
        <CartesianGrid stroke={colors.grid} strokeDasharray="2 4" vertical={false} />
        <XAxis dataKey="t" tickFormatter={clock} minTickGap={44} {...axisProps(colors)} />
        <YAxis width={54} {...axisProps(colors)} />
        <Tooltip content={<TooltipBox unit="kW" digits={1} />} cursor={{ stroke: colors.axis }} />
        <Line
          type="stepAfter"
          dataKey="v"
          stroke={SIGNAL_HEX.hold}
          strokeWidth={1.75}
          isAnimationActive={false}
          dot={false}
          activeDot={{ strokeWidth: 2, r: 4, stroke: colors.dotRing }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function SocChart({ series }: { series: SeriesPoint[] }) {
  const colors = useChartColors();
  if (series.length < 2) return <EmptyState title="No battery readings yet" />;
  const data = series.map((p) => ({ t: p.t, v: p.v }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
        <defs>
          <linearGradient id="socFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SIGNAL_HEX.wait} stopOpacity={0.3} />
            <stop offset="100%" stopColor={SIGNAL_HEX.wait} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={colors.grid} strokeDasharray="2 4" vertical={false} />
        <XAxis dataKey="t" tickFormatter={clock} minTickGap={44} {...axisProps(colors)} />
        <YAxis width={54} domain={[0, 100]} {...axisProps(colors)} />
        <Tooltip content={<TooltipBox unit="%" digits={1} />} cursor={{ stroke: colors.axis }} />
        <Area
          type="monotone"
          dataKey="v"
          stroke={SIGNAL_HEX.wait}
          strokeWidth={1.75}
          fill="url(#socFill)"
          isAnimationActive={false}
          dot={false}
          activeDot={{ strokeWidth: 2, r: 4, stroke: colors.dotRing }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** Hourly totals.
 *
 *  Deliberately identical in form to the daily chart: same shape, same
 *  colour, only the bucket changes. Two charts that answer the same question
 *  at different resolutions should look the same, so the eye can compare them
 *  without re-learning the encoding.
 */
export function HourlyEnergyChart({
  data,
}: {
  data: { hour: string; kwh: number }[];
}) {
  const colors = useChartColors();
  if (!data.length) {
    return (
      <EmptyState
        title="No hourly readings yet"
        hint="Bars appear once two meter readings exist in the same hour."
      />
    );
  }
  const ordered = [...data].reverse();

  return (
    <ResponsiveContainer width="100%" height={140}>
      <AreaChart data={ordered} margin={{ top: 8, right: 8, bottom: 0, left: -22 }}>
        <defs>
          <linearGradient id="hourlyFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SIGNAL_HEX.live} stopOpacity={0.28} />
            <stop offset="100%" stopColor={SIGNAL_HEX.live} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={colors.grid} strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey="hour"
          tickFormatter={(h: string) => `${h.slice(11)}:00`}
          minTickGap={28}
          {...axisProps(colors)}
        />
        <YAxis width={48} {...axisProps(colors)} />
        <Tooltip content={<TooltipBox unit="kWh" />} cursor={{ stroke: colors.axis }} />
        <Area
          type="monotone"
          dataKey="kwh"
          stroke={SIGNAL_HEX.live}
          strokeWidth={1.75}
          fill="url(#hourlyFill)"
          isAnimationActive={false}
          dot={false}
          activeDot={{ strokeWidth: 2, r: 4, stroke: colors.dotRing }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function UptimeTooltipBox({
  active,
  payload,
}: {
  active?: boolean;
  payload?: {
    payload: { startLabel: string; endLabel: string; durationLabel: string; word: string };
  }[];
}) {
  if (!active || !payload?.length) return null;
  const { startLabel, endLabel, durationLabel, word } = payload[0].payload;
  return (
    <div className="rounded-lg border border-line bg-panel-high px-2.5 py-1.5 shadow-panel">
      <p className="tnum text-xs text-ink-faint">{startLabel}</p>
      <p className="tnum text-xs text-ink-faint">{endLabel}</p>
      <p className="tnum mt-0.5 text-sm text-ink">
        {durationLabel}
        <span className="ml-1 text-xs text-ink-faint">{word}</span>
      </p>
    </div>
  );
}

/** Real connect/disconnect segments as vertical bars -- not artificial hourly
 *  or daily buckets. Each bar is one genuine stretch of being online,
 *  offline, or (before any history existed) unknown, labelled with its real
 *  start time rather than a rounded time-of-day mark, since a segment can
 *  begin at any moment and rounding it would misstate when it actually
 *  started. Height is the segment's own duration, so a short blip reads as a
 *  short bar and a long stretch as a tall one -- nothing is padded to a
 *  minimum size the way a fixed bucket would have to be.
 *
 *  The Y axis is fixed to the true window (48h or 7d), not to the tallest
 *  segment present -- two charts covering different real spans need
 *  different real scales, or "48 hours" and "7 days" end up looking like the
 *  same amount of time.
 */
export function UptimeBarChart({
  segments,
  windowHours,
}: {
  segments: UptimeSegment[];
  windowHours: 48 | 168;
}) {
  const colors = useChartColors();
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  if (!segments.length) {
    return (
      <EmptyState
        title="No connection history yet"
        hint="This fills in once the charger has been connecting for a while."
      />
    );
  }

  const data = segments.map((seg) => {
    const startMs = new Date(seg.start).getTime();
    const endMs = new Date(seg.end).getTime();
    const minutes = Math.max(0, (endMs - startMs) / 60000);
    const word = seg.connected == null ? "unknown" : seg.connected ? "online" : "offline";
    const color =
      seg.connected == null
        ? colors.tick
        : seg.connected
          ? SIGNAL_HEX.live
          : SIGNAL_HEX.fault;
    const dateOpts = { month: "short", day: "numeric" } as const;
    const timeOpts = { hour: "2-digit", minute: "2-digit" } as const;
    const tooltipTimeOpts = { hour: "2-digit", minute: "2-digit", second: "2-digit" } as const;
    return {
      minutes,
      color,
      word,
      startLabel:
        new Date(seg.start).toLocaleTimeString([], tooltipTimeOpts) +
        " · " +
        new Date(seg.start).toLocaleDateString([], dateOpts),
      endLabel:
        new Date(seg.end).toLocaleTimeString([], tooltipTimeOpts) +
        " · " +
        new Date(seg.end).toLocaleDateString([], dateOpts),
      // Compact axis tick: just the time, angled, so far more of them fit
      // along the bottom without Recharts silently dropping most of them for
      // lack of room.
      axisLabel: new Date(seg.start).toLocaleTimeString([], timeOpts),
      durationLabel: minutesLabel(minutes),
    };
  });

  return (
    <ResponsiveContainer width="100%" height={170}>
      <BarChart
        data={data}
        margin={{ top: 8, right: 8, bottom: 28, left: -22 }}
        // Tracked here, at the chart level, rather than per-Cell: Recharts
        // resolves this from mouse position across a bar's entire column
        // width and the chart's full plot height, not just where that bar's
        // own fill happens to be drawn. A short bar otherwise has almost no
        // hoverable area, and the empty space above it inside the same
        // column did nothing.
        onMouseMove={(state) => {
          const idx = state?.activeTooltipIndex;
          setHoveredIndex(typeof idx === "number" ? idx : null);
        }}
        onMouseLeave={() => setHoveredIndex(null)}
      >
        <XAxis
          dataKey="axisLabel"
          interval={0}
          angle={-60}
          textAnchor="end"
          height={40}
          {...axisProps(colors)}
        />
        <YAxis
          width={56}
          domain={[0, windowHours * 60]}
          ticks={[0, (windowHours * 60) / 2, windowHours * 60]}
          tickFormatter={minutesLabel}
          label={{
            value: windowHours === 48 ? "of 48h" : "of 7d",
            angle: -90,
            position: "insideLeft",
            fill: colors.tick,
            fontSize: 10,
            fontFamily: "JetBrains Mono",
          }}
          {...axisProps(colors)}
        />
        <Tooltip
          content={<UptimeTooltipBox />}
          cursor={{ fill: colors.grid }}
          active={hoveredIndex != null}
        />
        <Bar dataKey="minutes" isAnimationActive={false} radius={[2, 2, 0, 0]}>
          {data.map((d, i) => {
            const isHovered = hoveredIndex === i;
            const isDimmed = hoveredIndex != null && !isHovered;
            return (
              <Cell
                key={i}
                fill={d.color}
                fillOpacity={isDimmed ? 0.35 : 1}
                stroke={isHovered ? colors.dotRing : "none"}
                strokeWidth={isHovered ? 1.5 : 0}
              />
            );
          })}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function minutesLabel(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours <= 24) {
    const rounded = Math.round(hours * 10) / 10;
    return `${rounded % 1 === 0 ? rounded.toFixed(0) : rounded.toFixed(1)}h`;
  }
  const days = hours / 24;
  const rounded = Math.round(days * 10) / 10;
  return `${rounded % 1 === 0 ? rounded.toFixed(0) : rounded.toFixed(1)}d`;
}
export function DailyEnergyChart({
  data,
}: {
  data: { day: string; kwh: number }[];
}) {
  const colors = useChartColors();
  if (!data.length) return <EmptyState title="No sessions recorded yet" />;
  const ordered = [...data].reverse();

  return (
    <ResponsiveContainer width="100%" height={140}>
      <AreaChart data={ordered} margin={{ top: 8, right: 8, bottom: 0, left: -22 }}>
        <defs>
          <linearGradient id="dailyFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SIGNAL_HEX.live} stopOpacity={0.28} />
            <stop offset="100%" stopColor={SIGNAL_HEX.live} stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis
          dataKey="day"
          tickFormatter={(d: string) => d.slice(5)}
          minTickGap={28}
          {...axisProps(colors)}
        />
        <YAxis width={48} {...axisProps(colors)} />
        <Tooltip content={<TooltipBox unit="kWh" scale={1} />} cursor={{ stroke: colors.axis }} />
        <Area
          type="monotone"
          dataKey="kwh"
          stroke={SIGNAL_HEX.live}
          strokeWidth={1.75}
          fill="url(#dailyFill)"
          isAnimationActive={false}
          dot={false}
          activeDot={{ strokeWidth: 2, r: 4, stroke: colors.dotRing }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** The fleet's online/offline split, as a clickable pie.
 *
 *  Clicking a slice filters the charger list to that group; clicking the
 *  same slice again clears the filter. This is the only chart in the file
 *  that is also a control, which is why it takes onSliceClick and an
 *  activeFilter to show which slice (if any) is currently selected.
 */
export function OnlineOfflinePieChart({
  online,
  offline,
  activeFilter,
  onSliceClick,
  height = 140,
}: {
  online: number;
  offline: number;
  activeFilter: "online" | "offline" | null;
  onSliceClick: (filter: "online" | "offline" | null) => void;
  height?: number;
}) {
  const colors = useChartColors();
  const data = [
    { key: "online" as const, label: "Online", value: online, color: SIGNAL_HEX.live },
    { key: "offline" as const, label: "Offline", value: offline, color: SIGNAL_HEX.fault },
  ];
  const total = online + offline;

  if (total === 0) {
    return (
      <div
        className="grid place-items-center text-eyebrow uppercase text-ink-faint"
        style={{ height }}
      >
        No chargers yet
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <PieChart>
        <Pie
          data={data}
          dataKey="value"
          nameKey="label"
          cx="50%"
          cy="50%"
          innerRadius={38}
          outerRadius={58}
          paddingAngle={0}
          isAnimationActive={false}
          onClick={(entry) => {
            const key = (entry as { key?: "online" | "offline" }).key;
            if (!key) return;
            onSliceClick(activeFilter === key ? null : key);
          }}
        >
          {data.map((slice) => (
            <Cell
              key={slice.key}
              fill={slice.color}
              stroke={colors.dotRing}
              strokeWidth={activeFilter === slice.key ? 2 : 0}
              fillOpacity={
                activeFilter === null || activeFilter === slice.key ? 1 : 0.35
              }
              cursor="pointer"
            />
          ))}
        </Pie>
        <Tooltip
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const p = payload[0].payload as { label: string; value: number };
            return (
              <div className="rounded-md border border-line bg-panel-raised px-2 py-1 text-xs shadow-panel">
                <span className="text-ink">{p.label}</span>
                <span className="tnum ml-1.5 text-ink-faint">{p.value}</span>
              </div>
            );
          }}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}

/** The shared "Fleet" panel: online/offline pie chart, click-to-filter,
 *  and a legend with live counts -- the same block on every page that
 *  lists chargers, so the fleet-at-a-glance view means the same thing and
 *  behaves the same way everywhere it appears. */
export function FleetPanel({
  online,
  offline,
  activeFilter,
  onFilterChange,
}: {
  online: number;
  offline: number;
  activeFilter: "online" | "offline" | null;
  onFilterChange: (f: "online" | "offline" | null) => void;
}) {
  return (
    <Panel className="flex flex-wrap items-center gap-4 px-4 py-6">
      <p className="eyebrow shrink-0">Fleet</p>
      <div className="w-[140px] shrink-0">
        <OnlineOfflinePieChart
          online={online}
          offline={offline}
          activeFilter={activeFilter}
          onSliceClick={onFilterChange}
          height={130}
        />
      </div>
      <div className="flex gap-2 text-xs text-ink-faint">
        <button
          type="button"
          onClick={() => onFilterChange(activeFilter === "online" ? null : "online")}
          className={cx(
            "flex items-center gap-1.5 rounded-md px-2 py-1",
            activeFilter === "online" && "bg-panel-high text-ink",
          )}
        >
          <span className="h-2 w-2 rounded-full bg-signal-live" /> Online {online}
        </button>
        <button
          type="button"
          onClick={() => onFilterChange(activeFilter === "offline" ? null : "offline")}
          className={cx(
            "flex items-center gap-1.5 rounded-md px-2 py-1",
            activeFilter === "offline" && "bg-panel-high text-ink",
          )}
        >
          <span className="h-2 w-2 rounded-full bg-signal-fault" /> Offline {offline}
        </button>
      </div>
    </Panel>
  );
}