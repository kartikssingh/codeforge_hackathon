"""Indexed max-heap behaviour."""

from __future__ import annotations

from aria.core.priority_queue import IndexedPriorityQueue


def test_orders_by_priority_descending():
    queue = IndexedPriorityQueue()
    queue.upsert("low", 10)
    queue.upsert("high", 100)
    queue.upsert("mid", 50)

    assert queue.peek() == "high"
    assert queue.ordered_keys() == ["high", "mid", "low"]


def test_ties_break_first_in_first_out():
    queue = IndexedPriorityQueue()
    queue.upsert("second", 100, order=200.0)
    queue.upsert("first", 100, order=100.0)
    queue.upsert("third", 100, order=300.0)

    assert queue.ordered_keys() == ["first", "second", "third"]


def test_upsert_moves_an_existing_key_without_duplicating_it():
    queue = IndexedPriorityQueue()
    queue.upsert("a", 10)
    queue.upsert("b", 20)
    queue.upsert("a", 99)

    assert len(queue) == 2
    assert queue.peek() == "a"
    assert queue.priority("a") == 99
    assert queue.ordered_keys().count("a") == 1


def test_discard_removes_from_ordering_and_pop():
    queue = IndexedPriorityQueue()
    queue.upsert("a", 10)
    queue.upsert("b", 20)

    assert queue.discard("b") is True
    assert queue.discard("b") is False
    assert "b" not in queue
    assert queue.pop() == "a"
    assert queue.pop() is None


def test_pop_drains_in_priority_order():
    queue = IndexedPriorityQueue()
    for index, priority in enumerate([5, 90, 40, 70]):
        queue.upsert(f"r{index}", priority, order=float(index))

    assert [queue.pop() for _ in range(4)] == ["r1", "r3", "r2", "r0"]
    assert len(queue) == 0


def test_survives_heavy_churn_without_leaking_entries():
    """Lazy-deleted entries must be compacted, not accumulated for ever."""
    queue = IndexedPriorityQueue()
    for i in range(500):
        queue.upsert(f"r{i}", float(i))
    for i in range(0, 500, 2):
        queue.discard(f"r{i}")
    for i in range(1, 500, 2):
        queue.upsert(f"r{i}", float(i) * 2)

    assert len(queue) == 250
    assert queue.peek() == "r499"
    assert len(queue._heap) < 400  # noqa: SLF001 - compaction is the point here
