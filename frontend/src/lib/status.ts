import type { ConnectorStatus, SessionState } from "./types";

/** Colour is meaning here, not decoration: every connector state maps to one
 *  signal colour, and that mapping is the same everywhere in the interface. */
export type Signal = "live" | "hold" | "idle" | "fault" | "wait";

export const SIGNAL_HEX: Record<Signal, string> = {
  live: "#22D3A5",
  hold: "#F4A93C",
  idle: "#6B7A99",
  fault: "#FF5C5C",
  wait: "#7C9CF5",
};

const CONNECTOR_SIGNAL: Record<ConnectorStatus, Signal> = {
  Available: "idle",
  Preparing: "wait",
  Charging: "live",
  SuspendedEVSE: "hold",
  SuspendedEV: "hold",
  Finishing: "wait",
  Reserved: "wait",
  Unavailable: "idle",
  Faulted: "fault",
};

const SESSION_SIGNAL: Record<SessionState, Signal> = {
  WAITING: "wait",
  ACTIVE: "live",
  PAUSED: "hold",
  COMPLETED: "idle",
  FAULTED: "fault",
};

export function connectorSignal(status: ConnectorStatus): Signal {
  return CONNECTOR_SIGNAL[status] ?? "idle";
}

export function sessionSignal(state: SessionState): Signal {
  return SESSION_SIGNAL[state] ?? "idle";
}

/** Tailwind classes per signal. Written out in full because Tailwind only
 *  keeps classes it can see as complete strings in the source. */
export const SIGNAL_TEXT: Record<Signal, string> = {
  live: "text-signal-live",
  hold: "text-signal-hold",
  idle: "text-signal-idle",
  fault: "text-signal-fault",
  wait: "text-signal-wait",
};

export const SIGNAL_BG: Record<Signal, string> = {
  live: "bg-signal-live",
  hold: "bg-signal-hold",
  idle: "bg-signal-idle",
  fault: "bg-signal-fault",
  wait: "bg-signal-wait",
};

export const SIGNAL_CHIP: Record<Signal, string> = {
  live: "bg-signal-live/10 text-signal-live border-signal-live/30",
  hold: "bg-signal-hold/10 text-signal-hold border-signal-hold/30",
  idle: "bg-signal-idle/10 text-signal-idle border-signal-idle/30",
  fault: "bg-signal-fault/10 text-signal-fault border-signal-fault/30",
  wait: "bg-signal-wait/10 text-signal-wait border-signal-wait/30",
};

/** Plain-language explanation of what a connector state means, written from
 *  the operator's side of the screen rather than the protocol's. */
export const CONNECTOR_MEANING: Record<ConnectorStatus, string> = {
  Available: "Free, nothing plugged in",
  Preparing: "Cable connected, waiting to start",
  Charging: "Delivering power",
  SuspendedEVSE: "Held at zero by the operator",
  SuspendedEV: "The car has paused charging",
  Finishing: "Session ended, cable still in",
  Reserved: "Held for a booking",
  Unavailable: "Offline or out of service",
  Faulted: "Reporting a fault",
};
