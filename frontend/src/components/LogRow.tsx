/** One OCPP frame, expandable to the exact bytes that crossed the wire.
 *
 *  Shared by the Protocol log and a session's own message list so the two are
 *  identical by construction: the same row, the same expand behaviour, the
 *  same reconstructed frame. A session simply passes the frames belonging to
 *  it; the row does not know or care which page it is on.
 */

import { useState } from "react";
import { Check, ChevronRight, Copy } from "lucide-react";

import { clock } from "../lib/format";
import type { LogRow as LogRowData } from "../lib/types";
import { cx } from "./ui";

function parsePayload(raw: string | null): unknown {
  if (!raw) return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/** Rebuild the frame exactly as it crossed the wire.
 *
 *  OCPP-J framing is positional:
 *    CALL        [2, uniqueId, action, payload]
 *    CALLRESULT  [3, uniqueId, payload]
 *    CALLERROR   [4, uniqueId, errorCode, errorDescription, errorDetails]
 *
 *  Showing the real array rather than a summary means what you read here is
 *  what was actually sent, which is the only thing worth having in a protocol
 *  log when a charger is misbehaving.
 */
function wireFrame(row: LogRowData): string {
  const payload = parsePayload(row.payload);
  let frame: unknown[];

  if (row.message_type_id === 2) {
    frame = [2, row.unique_id, row.action, payload ?? {}];
  } else if (row.message_type_id === 3) {
    frame = [3, row.unique_id, payload ?? {}];
  } else {
    frame = [
      4,
      row.unique_id,
      row.error_code,
      row.error_description ?? "",
      parsePayload(row.error_details ?? null) ?? {},
    ];
  }
  return JSON.stringify(frame, null, 2);
}

export function LogRow({
  row,
  open,
  onToggle,
}: {
  row: LogRowData;
  open: boolean;
  onToggle: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const failed = Boolean(row.error_code);
  const isResult = row.message_type_id === 3;
  const inbound = row.direction === "INBOUND";
  const body = wireFrame(row);
  // The payload verbatim, including "{}" -- several .conf messages are
  // defined to carry no fields, and an empty object is the correct thing to
  // see for those rather than a word standing in for it.
  const summary = row.error_description ?? row.payload ?? "{}";

  async function copy(event: React.MouseEvent) {
    event.stopPropagation();
    await navigator.clipboard.writeText(body);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

  return (
    <li className="border-b border-line/50 last:border-0">
      {/* One uniform line, always. Everything that could vary in height lives
          in the expanded body below. */}
      <button
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-3 py-2 text-left font-mono text-xs hover:bg-panel-high/40"
      >
        <ChevronRight
          size={12}
          className={cx(
            "shrink-0 text-ink-faint transition-transform",
            open && "rotate-90",
          )}
        />
        <span className="shrink-0 text-ink-faint">{clock(row.timestamp)}</span>

        {/* A filled chip, like every other status indicator in the app, so
            direction reads at a glance rather than as bare text. Fixed width
            keeps the columns after it aligned. */}
        <span
          className={cx(
            "flex w-16 shrink-0 items-center justify-center gap-1 rounded px-1.5 py-0.5 text-eyebrow uppercase",
            inbound
              ? "bg-signal-wait/10 text-signal-wait"
              : "bg-signal-live/10 text-signal-live",
          )}
        >
          {inbound ? "◀ In" : "Out ▶"}
        </span>

        <span className="w-16 shrink-0 truncate text-ink-faint">
          {row.charge_point_id ?? "—"}
        </span>
        <span
          className={cx(
            "w-48 shrink-0 truncate",
            failed ? "text-signal-fault" : "text-ink",
          )}
        >
          {row.error_code ?? row.action ?? "unmatched result"}
          {isResult && !row.error_code && (
            <span className="ml-1.5 text-ink-faint">·reply</span>
          )}
        </span>
        <span className="min-w-0 flex-1 truncate text-ink-faint">{summary}</span>
      </button>

      {open && (
        <div className="relative border-t border-line/50 bg-panel/60 px-3 py-2.5">
          <button
            onClick={copy}
            title="Copy the frame"
            className="absolute right-2 top-2 flex items-center gap-1 rounded border border-line bg-panel-high px-2 py-1 text-xs text-ink-faint hover:text-ink"
          >
            {copied ? <Check size={11} /> : <Copy size={11} />}
            {copied ? "Copied" : "Copy"}
          </button>

          <p className="eyebrow mb-2">Frame as sent</p>

          <dl className="mb-2 flex flex-wrap gap-x-6 gap-y-1 text-xs">
            <div>
              <dt className="inline text-ink-faint">Message ID </dt>
              <dd className="tnum inline text-ink-dim">{row.unique_id ?? "—"}</dd>
            </div>
            <div>
              <dt className="inline text-ink-faint">Type </dt>
              <dd className="tnum inline text-ink-dim">
                {row.message_type_id}
                {row.message_type_id === 2
                  ? " · CALL"
                  : row.message_type_id === 3
                    ? " · CALLRESULT"
                    : " · CALLERROR"}
              </dd>
            </div>
          </dl>

          {failed && (
            <p className="mb-2 rounded-md border border-signal-fault/30 bg-signal-fault/10 px-2 py-1 font-mono text-xs text-signal-fault">
              {row.error_code}
              {row.error_description ? `: ${row.error_description}` : ""}
            </p>
          )}

          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all pr-20 font-mono text-xs leading-relaxed text-ink-dim">
            {body}
          </pre>
        </div>
      )}
    </li>
  );
}