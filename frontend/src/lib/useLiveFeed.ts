/** Subscribes to /ws/dashboard and turns events into cache invalidations.
 *
 *  The socket is the source of "something changed"; React Query remains the
 *  source of truth for what the data actually is. That split means a dropped
 *  socket degrades to the polling intervals rather than to a stale screen.
 */

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { LiveEvent } from "./types";

const MAX_EVENTS = 200;

export interface LiveFeed {
  connected: boolean;
  events: LiveEvent[];
}

export function useLiveFeed(): LiveFeed {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const retry = useRef(1000);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;

    const connect = () => {
      const base = import.meta.env.VITE_API_BASE;
      const url = base
        ? base.replace(/^http/, "ws") + "/ws/dashboard"
        : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard`;

      socket = new WebSocket(url);

      socket.onopen = () => {
        setConnected(true);
        retry.current = 1000;
      };

      socket.onmessage = (message) => {
        let event: LiveEvent;
        try {
          event = JSON.parse(message.data);
        } catch {
          return;
        }
        if (event.topic === "connected") return;

        setEvents((prev) => [event, ...prev].slice(0, MAX_EVENTS));

        // Refetch only what this event could have changed.
        if (event.topic.startsWith("session.")) {
          qc.invalidateQueries({ queryKey: ["overview"] });
          qc.invalidateQueries({ queryKey: ["sessions"] });
          qc.invalidateQueries({ queryKey: ["session"] });
        } else if (event.topic === "connector.status") {
          qc.invalidateQueries({ queryKey: ["overview"] });
          qc.invalidateQueries({ queryKey: ["charge-point"] });
        } else if (event.topic === "meter.values") {
          qc.invalidateQueries({ queryKey: ["overview"] });
          qc.invalidateQueries({ queryKey: ["session"] });
        } else if (event.topic.startsWith("chargepoint.")) {
          qc.invalidateQueries({ queryKey: ["overview"] });
          qc.invalidateQueries({ queryKey: ["charge-points"] });
          qc.invalidateQueries({ queryKey: ["charge-point"] });
        } else if (event.topic === "message.logged") {
          qc.invalidateQueries({ queryKey: ["logs"] });
        } else if (event.topic.startsWith("schedule.")) {
          qc.invalidateQueries({ queryKey: ["schedules"] });
        }
      };

      const scheduleReconnect = () => {
        setConnected(false);
        if (closed) return;
        timer = window.setTimeout(connect, retry.current);
        retry.current = Math.min(retry.current * 2, 15000);
      };

      socket.onclose = scheduleReconnect;
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socket?.close();
    };
  }, [qc]);

  return { connected, events };
}
