/** Light or dark, remembered across reloads.
 *
 *  The choice is written to localStorage and applied by toggling a class on
 *  the document root, which flips the CSS variables every component already
 *  reads. No component knows the theme changed -- they just recolour.
 *
 *  Reading and writing happen outside React so the correct theme is on the
 *  page before the first paint; a hook then exposes it for the toggle button.
 */

import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light";

const KEY = "charge-control-theme";
const listeners = new Set<() => void>();

function read(): Theme {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // Private mode or a sandbox with storage disabled: fall through to dark.
  }
  return "dark";
}

/** Put the class on <html> so the variables apply to everything, including
 *  portals and the scrollbar, which live outside the React tree. */
function apply(theme: Theme): void {
  document.documentElement.classList.toggle("theme-light", theme === "light");
}

let current: Theme = read();
apply(current);

export function setTheme(theme: Theme): void {
  current = theme;
  apply(theme);
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Nothing to do -- it simply will not persist, which is acceptable.
  }
  listeners.forEach((fn) => fn());
}

export function toggleTheme(): void {
  setTheme(current === "dark" ? "light" : "dark");
}

export function useTheme(): Theme {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => current,
    () => "dark",
  );
}