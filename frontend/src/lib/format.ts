/** Presentation helpers. Everything numeric goes through here so units and
 *  precision stay consistent across the whole interface. */

export function kwh(wh: number | null | undefined, digits = 2): string {
  if (wh == null) return "—";
  return (wh / 1000).toFixed(digits);
}

export function kw(w: number | null | undefined, digits = 1): string {
  if (w == null) return "—";
  return (w / 1000).toFixed(digits);
}

export function pct(value: number | null | undefined, digits = 0): string {
  if (value == null) return "—";
  return `${value.toFixed(digits)}%`;
}

/** Compact elapsed time: 4m, 1h 12m, 2d 3h. */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "—";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function since(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  return duration((Date.now() - then) / 1000);
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function datetime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** An ISO-8601 UTC string in the exact format the API expects. */
export function toApiTime(local: Date): string {
  return local.toISOString().replace("Z", "").slice(0, 23) + "Z";
}
