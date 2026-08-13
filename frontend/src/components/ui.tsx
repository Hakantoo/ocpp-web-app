/** Small primitives. Hand-written rather than pulled from a component library
 *  so the panel vocabulary (eyebrows, signal chips, instrument readouts) stays
 *  consistent and there is nothing to override. */

import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Search, Star } from "lucide-react";

import { SIGNAL_CHIP, SIGNAL_BG, type Signal } from "../lib/status";
import type { Container } from "../lib/containers";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// ---------------------------------------------------------------------------

export function Panel({
  children,
  className,
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article";
}) {
  return (
    <Tag
      className={cx(
        "rounded-xl border border-line bg-panel-raised shadow-panel",
        className,
      )}
    >
      {children}
    </Tag>
  );
}

export function PanelHeader({
  eyebrow,
  title,
  right,
}: {
  eyebrow?: string;
  title: ReactNode;
  right?: ReactNode;
}) {
  return (
    <header className="flex items-start justify-between gap-4 border-b border-line px-4 py-3">
      <div className="min-w-0">
        {eyebrow && <p className="eyebrow mb-1">{eyebrow}</p>}
        <h2 className="truncate text-sm font-semibold text-ink">{title}</h2>
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </header>
  );
}

// ---------------------------------------------------------------------------

export function Chip({
  signal,
  children,
  pip = false,
}: {
  signal: Signal;
  children: ReactNode;
  pip?: boolean;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5",
        "text-eyebrow font-medium uppercase",
        SIGNAL_CHIP[signal],
      )}
    >
      {pip && (
        <span
          className={cx(
            "h-1.5 w-1.5 rounded-full",
            SIGNAL_BG[signal],
            signal === "live" && "animate-pip",
          )}
        />
      )}
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------

type ButtonVariant = "primary" | "hold" | "danger" | "ghost";

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-signal-live/15 text-signal-live border-signal-live/40 hover:bg-signal-live/25",
  hold: "bg-signal-hold/15 text-signal-hold border-signal-hold/40 hover:bg-signal-hold/25",
  danger:
    "bg-signal-fault/15 text-signal-fault border-signal-fault/40 hover:bg-signal-fault/25",
  ghost:
    "bg-panel-high text-ink-dim border-line hover:bg-line/60 hover:text-ink hover:border-line-bright",
};

export function Button({
  children,
  onClick,
  variant = "ghost",
  disabled,
  busy,
  title,
  className,
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: ButtonVariant;
  disabled?: boolean;
  busy?: boolean;
  title?: string;
  className?: string;
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      title={title}
      disabled={disabled || busy}
      onClick={onClick}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg border px-3 py-1.5",
        "text-xs font-medium",
        "disabled:cursor-not-allowed disabled:opacity-40",
        BUTTON_VARIANTS[variant],
        className,
      )}
    >
      {busy ? <Spinner /> : children}
    </button>
  );
}

function Spinner() {
  return (
    <span className="h-3 w-3 animate-spin rounded-full border border-current border-t-transparent" />
  );
}

// ---------------------------------------------------------------------------

/** A labelled instrument reading. The number is the loud part; the unit and
 *  label stay quiet so a wall of these is still scannable. */
export function Readout({
  label,
  value,
  unit,
  signal,
  className,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  signal?: Signal;
  className?: string;
}) {
  return (
    <div className={className}>
      <p className="eyebrow mb-1">{label}</p>
      <p
        className={cx(
          "tnum text-xl font-medium leading-none",
          signal ? SIGNAL_CHIP[signal].split(" ")[1] : "text-ink",
        )}
      >
        {value}
        {unit && <span className="ml-1 text-xs text-ink-faint">{unit}</span>}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      <p className="text-sm font-medium text-ink-dim">{title}</p>
      {hint && <p className="max-w-sm text-xs text-ink-faint">{hint}</p>}
      {action}
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <p className="rounded-lg border border-signal-fault/30 bg-signal-fault/10 px-3 py-2 text-xs text-signal-fault">
      {message}
    </p>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div className={cx("relative overflow-hidden rounded-lg bg-panel-high", className)}>
      <div className="absolute inset-y-0 w-1/3 animate-sweep bg-gradient-to-r from-transparent via-white/5 to-transparent" />
    </div>
  );
}

// ---------------------------------------------------------------------------

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">{children}</table>
    </div>
  );
}

export function Th({
  children,
  className,
}: {
  children?: ReactNode;
  className?: string;
}) {
  return (
    <th
      className={cx(
        "border-b border-line px-4 py-2 text-eyebrow uppercase text-ink-faint",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <td className={cx("border-b border-line/60 px-4 py-2.5 text-ink-dim", className)}>
      {children}
    </td>
  );
}

// ---------------------------------------------------------------------------
// Form primitives
//
// Kept deliberately plain: these back the directory editors, where the useful
// thing is a predictable field that reports its own error, not styling.
// ---------------------------------------------------------------------------

export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cx("block", className)}>
      <span className="eyebrow mb-1.5 block">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-faint">{hint}</span>}
    </label>
  );
}

const CONTROL =
  "w-full rounded-lg border border-line bg-panel px-2.5 py-1.5 text-sm text-ink " +
  "placeholder:text-ink-faint focus:border-signal-live/50";

export function Input({
  value,
  onChange,
  placeholder,
  type = "text",
  min,
  max,
  step,
  disabled,
}: {
  value: string | number;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: "text" | "number" | "email" | "datetime-local";
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}) {
  return (
    <input
      type={type}
      value={value}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className={cx(CONTROL, type === "number" && "tnum")}
    />
  );
}

export function Select({
  value,
  onChange,
  options,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string; disabled?: boolean }[];
  disabled?: boolean;
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={CONTROL}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value} disabled={option.disabled}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

/** A dialog. Closes on Escape and on backdrop click, and traps nothing --
 *  these forms are small enough that native tab order is correct already. */
export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-panel/80 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onClose}
      onKeyDown={(e) => e.key === "Escape" && onClose()}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-md animate-rise flex-col rounded-xl border border-line bg-panel-raised shadow-panel"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="shrink-0 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
        </header>
        <div className="space-y-3 overflow-y-auto px-4 py-4">{children}</div>
        {footer && (
          <footer className="flex shrink-0 justify-end gap-2 border-t border-line px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

/** Transient confirmation or failure, shown next to the control that caused
 *  it rather than in a corner of the screen. */
export function Note({
  tone,
  children,
}: {
  tone: "ok" | "error" | "info";
  children: ReactNode;
}) {
  const tones = {
    ok: "border-signal-live/30 bg-signal-live/10 text-signal-live",
    error: "border-signal-fault/30 bg-signal-fault/10 text-signal-fault",
    info: "border-signal-wait/30 bg-signal-wait/10 text-signal-wait",
  } as const;
  return (
    <p className={cx("rounded-lg border px-3 py-2 text-xs", tones[tone])}>{children}</p>
  );
}

/** A plain, contains-match search box for filtering a charger list.
 *
 *  Deliberately not a dropdown with keyboard navigation like the Protocol
 *  Log's own charger picker -- that one exists to *pick* a single charger
 *  out of a message stream; this one exists to narrow a list you are
 *  already looking at. A live text filter is simpler and is what was
 *  actually asked for; no wildcard syntax, just a case-insensitive
 *  substring match against identity and label.
 */
export function ChargerSearch({
  value,
  onChange,
  placeholder = "Search chargers",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="relative">
      <Search
        size={13}
        className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint"
      />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-lg border border-line bg-panel py-1.5 pl-8 pr-3 text-xs text-ink placeholder:text-ink-faint focus:outline-none"
      />
    </div>
  );
}

/** A star toggle for pinning a charger to the top of its list.
 *
 *  Stops event propagation on click, since this always sits inside a
 *  clickable card header (the collapse toggle) and starring a charger
 *  should never also expand or collapse it.
 */
export function FavoriteStar({
  active,
  onToggle,
  label,
}: {
  active: boolean;
  onToggle: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      aria-label={active ? `Unstar ${label}` : `Star ${label}`}
      aria-pressed={active}
      className={cx(
        "shrink-0 rounded-md p-1",
        active ? "text-signal-hold" : "text-ink-faint hover:text-ink-dim",
      )}
    >
      <Star size={14} fill={active ? "currentColor" : "none"} />
    </button>
  );
}

/** A small popover for picking an existing container or creating a new one
 *  on the spot. Rendered through a portal into document.body, anchored to
 *  the real screen position of whatever button opened it -- this is what
 *  lets it escape a collapsible card's own overflow-hidden, which silently
 *  clipped it when it was just position:absolute inside the card. */
export function ContainerPickerPortal({
  anchor,
  containers,
  onPick,
  onCreate,
  onClose,
}: {
  anchor: HTMLElement;
  containers: Container[];
  onPick: (id: string) => void;
  onCreate: (name: string) => boolean;
  onClose: () => void;
}) {
  const [newName, setNewName] = useState("");
  const [duplicateError, setDuplicateError] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);
  const rect = anchor.getBoundingClientRect();

  function attemptCreate() {
    const trimmed = newName.trim();
    if (!trimmed) return;
    const ok = onCreate(trimmed);
    if (!ok) {
      setDuplicateError(true);
      return;
    }
    setNewName("");
    setDuplicateError(false);
  }

  useEffect(() => {
    const onClickAway = (e: MouseEvent) => {
      const target = e.target as Node;
      if (anchor.contains(target)) return;
      if (popoverRef.current?.contains(target)) return;
      onClose();
    };
    // Deferred one tick so the click that opened this does not also close it.
    const id = setTimeout(() => document.addEventListener("mousedown", onClickAway));
    return () => {
      clearTimeout(id);
      document.removeEventListener("mousedown", onClickAway);
    };
  }, [anchor, onClose]);

  return createPortal(
    <div
      ref={popoverRef}
      style={{
        position: "fixed",
        top: rect.bottom + 4,
        left: Math.max(8, rect.right - 208),
        zIndex: 50,
      }}
      className="w-52 rounded-lg border border-line bg-panel-raised p-2 shadow-panel"
      onClick={(e) => e.stopPropagation()}
    >
      {containers.length > 0 && (
        <div className="mb-2 max-h-40 space-y-0.5 overflow-y-auto">
          {containers.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => onPick(c.id)}
              className="block w-full truncate rounded-md px-2 py-1 text-left text-xs text-ink-dim hover:bg-panel-high hover:text-ink"
            >
              {c.name}
            </button>
          ))}
        </div>
      )}
      {duplicateError && (
        <p className="mb-1.5 text-xs text-signal-fault">
          A container with that name already exists.
        </p>
      )}
      <div className="flex gap-1">
        <input
          autoFocus
          value={newName}
          onChange={(e) => {
            setNewName(e.target.value);
            setDuplicateError(false);
          }}
          placeholder="New container"
          className="w-full rounded-md border border-line bg-panel px-2 py-1 text-xs text-ink placeholder:text-ink-faint focus:outline-none"
          onKeyDown={(e) => {
            if (e.key === "Enter") attemptCreate();
            if (e.key === "Escape") onClose();
          }}
        />
        <button
          type="button"
          onClick={attemptCreate}
          className="shrink-0 rounded-md border border-line px-2 text-xs text-ink-dim hover:text-ink"
        >
          Add
        </button>
      </div>
    </div>,
    document.body,
  );
}