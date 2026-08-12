/** Favorite chargers, remembered per-browser.
 *
 *  Local only, on purpose: a starred charger is a personal shortcut for
 *  whoever is looking at this browser right now, not a fact about the
 *  charger itself that other operators should see too. Saving it to
 *  localStorage keeps this simple and needs no backend change; the
 *  tradeoff is that it does not follow you to a different browser or
 *  device, which is an acceptable cost for what this is.
 */

import { useCallback, useEffect, useState } from "react";

const KEY = "charge-control:favorite-chargers";

function readFavorites(): Set<string> {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed) : new Set();
  } catch {
    // Private browsing, storage disabled, or corrupted data -- fall back to
    // no favorites rather than let this break the page.
    return new Set();
  }
}

function writeFavorites(favorites: Set<string>): void {
  try {
    localStorage.setItem(KEY, JSON.stringify([...favorites]));
  } catch {
    // Storage unavailable -- the toggle still works for this page load,
    // it just will not survive a reload. Not worth surfacing an error for.
  }
}

export interface Favorites {
  isFavorite: (identity: string) => boolean;
  toggleFavorite: (identity: string) => void;
}

export function useFavorites(): Favorites {
  const [favorites, setFavorites] = useState<Set<string>>(() => readFavorites());

  // Pick up changes made in another tab -- two dashboard tabs starring
  // different chargers should not silently fight each other's writes.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY) setFavorites(readFavorites());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const toggleFavorite = useCallback((identity: string) => {
    setFavorites((prev) => {
      const next = new Set(prev);
      if (next.has(identity)) next.delete(identity);
      else next.add(identity);
      writeFavorites(next);
      return next;
    });
  }, []);

  const isFavorite = useCallback(
    (identity: string) => favorites.has(identity),
    [favorites],
  );

  return { isFavorite, toggleFavorite };
}

/** Favorites first, alphabetical within each group -- the sort every page
 *  using favorites should share, so starring a charger means the same thing
 *  everywhere it appears. */
export function sortByFavorite<T>(
  items: T[],
  identityOf: (item: T) => string,
  isFavorite: (identity: string) => boolean,
): T[] {
  return [...items].sort((a, b) => {
    const favA = isFavorite(identityOf(a));
    const favB = isFavorite(identityOf(b));
    if (favA !== favB) return favA ? -1 : 1;
    return identityOf(a).localeCompare(identityOf(b));
  });
}
