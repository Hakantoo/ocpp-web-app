/** Application shell: a left instrument rail and a status header.
 *
 *  The header pip is the one piece of ambient motion in the interface. It
 *  reports whether the dashboard's own event socket is up, which is different
 *  from whether any charger is connected -- an operator needs to tell those
 *  two failures apart.
 */

import { NavLink, Outlet } from "react-router-dom";
import {
  Moon,
  Sun,
  Activity,
  BatteryCharging,
  Cpu,
  LayoutGrid,
  Radio,
  ScrollText,
  CreditCard,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { useLiveFeed } from "../lib/useLiveFeed";
import { toggleTheme, useTheme } from "../lib/theme";
import { UnknownCardToast } from "./UnknownCardToast";
import { cx } from "./ui";

const NAV: { to: string; label: string; icon: LucideIcon; end?: boolean }[] = [
  { to: "/", label: "Overview", icon: LayoutGrid, end: true },
  { to: "/chargers", label: "Chargers", icon: Cpu },
  { to: "/sessions", label: "Sessions", icon: BatteryCharging },
  { to: "/directory", label: "Cards & cars", icon: CreditCard },
  { to: "/logs", label: "Protocol log", icon: ScrollText },
  { to: "/simulator", label: "Simulator", icon: Radio },
];

export function Layout() {
  const { connected, events } = useLiveFeed();

  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <UnknownCardToast events={events} />
      {/* Rail */}
      <aside className="flex shrink-0 flex-col border-b border-line bg-panel-raised lg:sticky lg:top-0 lg:h-screen lg:w-56 lg:border-b-0 lg:border-r">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <span className="grid h-8 w-8 place-items-center rounded-lg border border-signal-live/40 bg-signal-live/10">
            <Activity size={16} className="text-signal-live" />
          </span>
          <div className="leading-tight">
            <p className="text-sm font-semibold tracking-tight">Charge Control</p>
            <p className="eyebrow">OCPP 1.6J</p>
          </div>
        </div>

        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:overflow-visible lg:pb-4">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cx(
                  "flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-sm",
                  isActive
                    ? "bg-signal-live/10 text-signal-live"
                    : "text-ink-dim hover:bg-panel-high hover:text-ink",
                )
              }
            >
              <Icon size={15} />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto hidden border-t border-line px-4 py-3 lg:block">
          <ThemeToggle />
          <div className="mt-2.5">
            <FeedStatus connected={connected} />
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-end gap-3 border-b border-line px-4 py-2.5 lg:hidden">
          <FeedStatus connected={connected} />
        </header>
        <main className="min-w-0 flex-1 p-4 lg:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

/** Light/dark switch. Shows the theme you would move *to*, which is the
 *  convention people expect from a single toggle. */
function ThemeToggle() {
  const theme = useTheme();
  const goingToLight = theme === "dark";
  return (
    <button
      onClick={toggleTheme}
      className="flex w-full items-center gap-2 rounded-md border border-line px-2.5 py-1.5 text-xs text-ink-dim hover:border-line-bright hover:text-ink"
      aria-label={`Switch to ${goingToLight ? "light" : "dark"} theme`}
    >
      {goingToLight ? <Sun size={13} /> : <Moon size={13} />}
      {goingToLight ? "Light" : "Dark"}
    </button>
  );
}

function FeedStatus({ connected }: { connected: boolean }) {
  return (
    <p className="flex items-center gap-2 text-xs text-ink-faint">
      <span
        className={cx(
          "h-1.5 w-1.5 rounded-full",
          connected ? "animate-pip bg-signal-live" : "bg-signal-fault",
        )}
      />
      {connected ? "Live feed connected" : "Reconnecting…"}
    </p>
  );
}