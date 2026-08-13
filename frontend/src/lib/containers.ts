/** Named groups of chargers, remembered per-browser.
 *
 *  Local only, same reasoning as favorites: a container is an operator's
 *  own way of organising what they are looking at, not a fact the backend
 *  needs to know. A charger belongs to at most one container -- moving it
 *  into a new one silently removes it from whatever it was in before.
 */

import { useCallback, useEffect, useState } from "react";

const KEY = "charge-control:charger-containers";

export interface Container {
  id: string;
  name: string;
  /** Charger identities in this container. */
  members: string[];
}

function readContainers(): Container[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeContainers(containers: Container[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(containers));
  } catch {
    // Storage unavailable -- the change still works for this page load.
  }
}

export interface Containers {
  containers: Container[];
  /** Which container (if any) a given charger currently belongs to. */
  containerOf: (identity: string) => Container | null;
  createContainer: (name: string, firstMember?: string) => string | null;
  renameContainer: (id: string, name: string) => void;
  deleteContainer: (id: string) => void;
  /** Moves a charger into this container, removing it from any other it
   *  was already in -- a charger is in at most one container at a time. */
  addToContainer: (containerId: string, identity: string) => void;
  removeFromContainer: (identity: string) => void;
}

export function useContainers(): Containers {
  const [containers, setContainers] = useState<Container[]>(() => readContainers());

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY) setContainers(readContainers());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const persist = useCallback((next: Container[]) => {
    setContainers(next);
    writeContainers(next);
  }, []);

  const createContainer = useCallback(
    (name: string, firstMember?: string) => {
      const trimmed = name.trim();
      const duplicate = containers.some(
        (c) => c.name.trim().toLowerCase() === trimmed.toLowerCase(),
      );
      if (duplicate) return null;
      const id = `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
      const members = firstMember ? [firstMember] : [];
      persist([...containers, { id, name: trimmed, members }]);
      return id;
    },
    [containers, persist],
  );

  const renameContainer = useCallback(
    (id: string, name: string) => {
      persist(containers.map((c) => (c.id === id ? { ...c, name } : c)));
    },
    [containers, persist],
  );

  const deleteContainer = useCallback(
    (id: string) => {
      persist(containers.filter((c) => c.id !== id));
    },
    [containers, persist],
  );

  const addToContainer = useCallback(
    (containerId: string, identity: string) => {
      persist(
        containers.map((c) => {
          if (c.id === containerId) {
            return c.members.includes(identity)
              ? c
              : { ...c, members: [...c.members, identity] };
          }
          // A charger is in at most one container -- drop it from any
          // other one it was previously a member of.
          return c.members.includes(identity)
            ? { ...c, members: c.members.filter((m) => m !== identity) }
            : c;
        }),
      );
    },
    [containers, persist],
  );

  const removeFromContainer = useCallback(
    (identity: string) => {
      persist(
        containers.map((c) =>
          c.members.includes(identity)
            ? { ...c, members: c.members.filter((m) => m !== identity) }
            : c,
        ),
      );
    },
    [containers, persist],
  );

  const containerOf = useCallback(
    (identity: string) => containers.find((c) => c.members.includes(identity)) ?? null,
    [containers],
  );

  return {
    containers,
    containerOf,
    createContainer,
    renameContainer,
    deleteContainer,
    addToContainer,
    removeFromContainer,
  };
}

/** Whether a charger should show up for a given search: matches its own
 *  identity/label directly, OR belongs to a container whose name matches,
 *  OR belongs to a container that has some other member matching -- typing
 *  "cp0006" surfaces cp0006's whole container even though nothing else in
 *  it mentions "cp0006", which is the behaviour actually asked for. */
export function matchesContainerSearch(
  identity: string,
  label: string | null,
  query: string,
  container: Container | null,
): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if (identity.toLowerCase().includes(q)) return true;
  if ((label ?? "").toLowerCase().includes(q)) return true;
  if (!container) return false;
  if (container.name.toLowerCase().includes(q)) return true;
  return container.members.some((m) => m.toLowerCase().includes(q));
}