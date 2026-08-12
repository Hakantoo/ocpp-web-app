/** A card nobody has recorded was just held to a reader.
 *
 *  This is the only moment the operator learns that a physical card exists:
 *  the number is on the wire once, the charger is told Invalid, and then it is
 *  gone. Catching it here turns "that card didn't work" into one click.
 *
 *  It dismisses itself after ten seconds, with the bar underneath showing how
 *  long is left, because an unread prompt should not accumulate on screen.
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CreditCard, X } from "lucide-react";

import type { LiveEvent } from "../lib/types";
import { Button } from "./ui";

const LIFETIME_MS = 10_000;

export function UnknownCardToast({ events }: { events: LiveEvent[] }) {
  const navigate = useNavigate();
  const [card, setCard] = useState<string | null>(null);
  const [remaining, setRemaining] = useState(LIFETIME_MS);
  // Cards already offered once. Without this, the same tap would reappear
  // every time the event list changes.
  const [seen] = useState(() => new Set<string>());

  useEffect(() => {
    const latest = events.find((e) => e.topic === "card.unknown");
    const id = latest?.id_tag as string | undefined;
    if (!id || seen.has(id)) return;
    seen.add(id);
    setCard(id);
    setRemaining(LIFETIME_MS);
  }, [events, seen]);

  useEffect(() => {
    if (!card) return;
    const started = Date.now();
    const tick = setInterval(() => {
      const left = LIFETIME_MS - (Date.now() - started);
      if (left <= 0) {
        setCard(null);
        setRemaining(0);
      } else {
        setRemaining(left);
      }
    }, 100);
    return () => clearInterval(tick);
  }, [card]);

  if (!card) return null;

  return (
    <div className="animate-rise fixed bottom-5 right-5 z-50 w-80 overflow-hidden rounded-lg border border-line bg-panel-high shadow-xl">
      <div className="flex items-start gap-3 px-4 pt-3.5">
        <CreditCard size={15} className="mt-0.5 shrink-0 text-signal-hold" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-ink">Unknown Card</p>
          <p className="tnum mt-0.5 truncate text-xs text-ink-dim">{card}</p>
        </div>
        <button
          onClick={() => setCard(null)}
          className="shrink-0 text-ink-faint hover:text-ink"
          aria-label="Dismiss"
        >
          <X size={13} />
        </button>
      </div>

      <p className="px-4 pb-3 pt-1.5 text-xs text-ink-faint">
        It was refused because nothing on file matches it.
      </p>

      <div className="flex gap-2 px-4 pb-3.5">
        <Button
          variant="primary"
          onClick={() => {
            setCard(null);
            // The Tags page opens its own add form when handed a card.
            navigate("/directory", { state: { addCard: card } });
          }}
        >
          View and add
        </Button>
        <Button onClick={() => setCard(null)}>Ignore</Button>
      </div>

      {/* How long is left, so the prompt is never a surprise disappearance. */}
      <div className="h-0.5 w-full bg-line">
        <div
          className="h-full bg-signal-hold transition-[width] duration-100 ease-linear"
          style={{ width: `${(remaining / LIFETIME_MS) * 100}%` }}
        />
      </div>
    </div>
  );
}