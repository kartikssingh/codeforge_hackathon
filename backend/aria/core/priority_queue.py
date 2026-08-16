"""An indexed max-heap with O(log n) update and removal.

Python's :mod:`heapq` has no decrease-key, and the previous implementation
worked around that by rebuilding the entire heap on every escalation tick and by
linear-scanning a sorted copy on every dispatch.  With escalation running once a
minute over every open request that is quadratic work on the busiest possible
day.

This is the standard "lazy deletion" recipe instead:

* each key owns exactly one heap entry, tracked in a dict;
* updating a key invalidates its old entry and pushes a fresh one;
* invalidated entries are skipped on pop and compacted when they pile up.

Entries are ``[-priority, order, seq, key]``:

* ``-priority`` turns Python's min-heap into a max-heap;
* ``order`` is the arrival timestamp, so equal keys are served first-in-first-out
  — the property that stops two equally critical cases from swapping places on
  every tick;
* ``seq`` is a monotonic counter that makes the comparison total, so the heap
  never falls through to comparing the string keys.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from typing import Any, Iterator, Optional

_REMOVED = object()

#: Compact once invalidated entries are both numerous and a majority.
_COMPACT_MIN = 64


class IndexedPriorityQueue:
    """Thread-safe max-heap of ``key -> priority`` with stable FIFO tie-breaks."""

    def __init__(self) -> None:
        self._heap: list[list[Any]] = []
        self._entries: dict[str, list[Any]] = {}
        self._counter = itertools.count()
        self._invalid = 0
        self._lock = threading.RLock()

    # ── Mutation ──────────────────────────────────────────────────────────────

    def upsert(self, key: str, priority: float, order: float = 0.0) -> None:
        """Insert *key*, or move it to a new *priority* if already present."""
        with self._lock:
            self.discard(key)
            entry: list[Any] = [-float(priority), float(order), next(self._counter), key]
            self._entries[key] = entry
            heapq.heappush(self._heap, entry)

    def discard(self, key: str) -> bool:
        """Remove *key* if present.  Returns whether anything was removed."""
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return False
            entry[3] = _REMOVED
            self._invalid += 1
            self._compact_if_needed()
            return True

    def clear(self) -> None:
        with self._lock:
            self._heap.clear()
            self._entries.clear()
            self._invalid = 0

    # ── Reads ─────────────────────────────────────────────────────────────────

    def peek(self) -> Optional[str]:
        """Highest-priority key without removing it."""
        with self._lock:
            self._drop_invalid_head()
            return self._heap[0][3] if self._heap else None

    def pop(self) -> Optional[str]:
        """Remove and return the highest-priority key."""
        with self._lock:
            self._drop_invalid_head()
            if not self._heap:
                return None
            entry = heapq.heappop(self._heap)
            key = entry[3]
            self._entries.pop(key, None)
            return key

    def priority(self, key: str) -> Optional[float]:
        with self._lock:
            entry = self._entries.get(key)
            return -entry[0] if entry else None

    def ordered_keys(self) -> list[str]:
        """Every key, most urgent first.  O(n log n), for the dashboard."""
        with self._lock:
            entries = list(self._entries.values())
        entries.sort(key=lambda e: (e[0], e[1], e[2]))
        return [e[3] for e in entries]

    def snapshot(self) -> list[tuple[str, float]]:
        with self._lock:
            entries = list(self._entries.values())
        entries.sort(key=lambda e: (e[0], e[1], e[2]))
        return [(e[3], -e[0]) for e in entries]

    # ── Dunder ────────────────────────────────────────────────────────────────

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self.ordered_keys())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IndexedPriorityQueue(size={len(self)}, invalid={self._invalid})"

    # ── Internals ─────────────────────────────────────────────────────────────

    def _drop_invalid_head(self) -> None:
        while self._heap and self._heap[0][3] is _REMOVED:
            heapq.heappop(self._heap)
            self._invalid -= 1

    def _compact_if_needed(self) -> None:
        if self._invalid >= _COMPACT_MIN and self._invalid * 2 >= len(self._heap):
            self._heap = [e for e in self._heap if e[3] is not _REMOVED]
            heapq.heapify(self._heap)
            self._invalid = 0
