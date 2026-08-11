"""In-process event bus.

The gateway publishes; the dashboard WebSocket and the scheduler subscribe.
Deliberately behind a Protocol so that swapping in Redis later is one new
class and one line in app.py -- nothing in the domain layer changes.

Delivery is best-effort and non-blocking: a slow subscriber gets its oldest
events dropped rather than stalling the OCPP message loop. Losing a UI
refresh is always preferable to delaying a charger's CALLRESULT.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from .db.database import now_db

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=now_db)

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "timestamp": self.timestamp, **self.data}


class EventBus(Protocol):
    async def publish(self, topic: str, **data: Any) -> None: ...
    def subscribe(self, *topics: str) -> "Subscription": ...


class Subscription:
    """An async iterator over events. Use as a context manager."""

    def __init__(self, bus: "InProcessEventBus", topics: tuple[str, ...], maxsize: int):
        self._bus = bus
        self.topics = topics
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)

    def matches(self, topic: str) -> bool:
        # "*" matches everything; "session.*" matches "session.started".
        return any(
            t == "*" or t == topic or (t.endswith("*") and topic.startswith(t[:-1]))
            for t in self.topics
        )

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()  # drop oldest, keep newest
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._bus.unsubscribe(self)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()


class InProcessEventBus:
    def __init__(self, queue_size: int = 200) -> None:
        self._subscribers: list[Subscription] = []
        self._queue_size = queue_size

    async def publish(self, topic: str, **data: Any) -> None:
        if not self._subscribers:
            return
        event = Event(topic=topic, data=data)
        for sub in list(self._subscribers):
            if sub.matches(topic):
                sub.offer(event)

    def subscribe(self, *topics: str) -> Subscription:
        sub = Subscription(self, topics or ("*",), self._queue_size)
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
