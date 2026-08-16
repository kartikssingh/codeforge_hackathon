"""In-process publish/subscribe used to push live updates to the dashboard.

The renderer used to poll ``/queue``, ``/volunteers`` and ``/inventory`` on
independent timers, which made the three panels disagree with each other for up
to three seconds and burned CPU on an idle shelter laptop.  Services now publish
a single event whenever state changes, and ``GET /events`` streams it to the
renderer over Server-Sent Events.

Publishers run on worker threads (the escalation loop, the pipeline threadpool),
consumers run on the asyncio loop, so the hand-off point is a bounded
:class:`queue.Queue` per subscriber.  A subscriber that stops draining (a frozen
window, a closed laptop lid) loses its oldest events instead of growing without
bound — the next full board refresh puts it back in sync anyway.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from aria.core.logging import get_logger
from aria.utils.timeutil import now

log = get_logger("eventbus")

#: Events a subscriber may receive.  Kept as plain strings on the wire.
EVENT_QUEUE_CHANGED = "queue.changed"
EVENT_VOLUNTEERS_CHANGED = "volunteers.changed"
EVENT_INVENTORY_CHANGED = "inventory.changed"
EVENT_REQUEST_CREATED = "request.created"
EVENT_REQUEST_UPDATED = "request.updated"
EVENT_ESCALATED = "request.escalated"
EVENT_HEARTBEAT = "heartbeat"


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "at": self.at, "payload": self.payload}


class Subscription:
    """One consumer's mailbox.  Use as a context manager."""

    def __init__(self, bus: "EventBus", maxsize: int = 256) -> None:
        self._bus = bus
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=maxsize)

    def deliver(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Drop the oldest so a stalled client cannot pin memory.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except (queue.Empty, queue.Full):  # pragma: no cover - racy edge
                pass

    def get_nowait(self) -> Optional[Event]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self, limit: int = 32) -> list[Event]:
        events: list[Event] = []
        while len(events) < limit:
            event = self.get_nowait()
            if event is None:
                break
            events.append(event)
        return events

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()
        self._lock = threading.Lock()

    def subscribe(self, maxsize: int = 256) -> Subscription:
        subscription = Subscription(self, maxsize=maxsize)
        with self._lock:
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)

    def publish(self, event_type: str, **payload: Any) -> Event:
        event = Event(type=event_type, payload=payload)
        with self._lock:
            targets = list(self._subscribers)
        for subscription in targets:
            subscription.deliver(event)
        return event

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def __iter__(self) -> Iterator[Subscription]:  # pragma: no cover - debugging
        with self._lock:
            return iter(list(self._subscribers))


#: Process-wide bus.  The hub owns it; routes read it through the hub.
event_bus = EventBus()
