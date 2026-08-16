"""Heap key formula and escalation policy."""

from __future__ import annotations

import pytest

from aria.domain.enums import RequestStatus, Severity
from aria.domain.priority import (
    PROMOTION_DELAY_HOURS,
    compute_heap_key,
    escalate,
    escalation_boost,
    next_severity,
)


def test_severity_scores_and_ranks():
    assert Severity.CRITICAL.score == 100
    assert Severity.LOW.score == 25
    assert Severity.CRITICAL.rank < Severity.HIGH.rank < Severity.LOW.rank


@pytest.mark.parametrize(
    "value,expected",
    [
        ("critical", Severity.CRITICAL),
        ("HIGH", Severity.HIGH),
        (" Medium ", Severity.MEDIUM),
        (100, Severity.CRITICAL),
        (25, Severity.LOW),
        ("nonsense", Severity.HIGH),  # pessimistic default
        (None, Severity.HIGH),
    ],
)
def test_severity_coercion(value, expected):
    assert Severity.from_any(value) is expected


def test_severity_dominates_travel_time():
    """A distant CRITICAL still outranks a next-door HIGH."""
    far_critical = compute_heap_key(100, travel_time_min=60, resolution_time_min=60)
    near_high = compute_heap_key(75, travel_time_min=0, resolution_time_min=0)
    assert far_critical > near_high


def test_travel_and_resolution_order_within_a_tier():
    quick = compute_heap_key(75, travel_time_min=5, resolution_time_min=10)
    slow = compute_heap_key(75, travel_time_min=20, resolution_time_min=40)
    assert quick > slow


def test_escalation_boost_uses_the_highest_crossed_tier_only():
    none_yet, stage = escalation_boost(Severity.LOW, hours_waited=1, travel_time_min=10, resolution_time_min=20)
    assert (none_yet, stage) == (0.0, 0)

    first, stage_one = escalation_boost(Severity.LOW, 6.5, 10, 20)
    second, stage_two = escalation_boost(Severity.LOW, 10.5, 10, 20)
    assert 0 < first < second
    assert stage_one == 1 and stage_two == 2


def test_promotion_requires_time_at_the_current_level():
    """The regression that mattered: LOW must not reach CRITICAL in three ticks.

    The old scheduler measured every threshold from the request time, so once a
    LOW request passed 6 h it satisfied MEDIUM's 4 h and HIGH's 3 h thresholds
    too and was promoted on three consecutive 60-second passes.
    """
    outcome = escalate(
        severity=Severity.LOW,
        hours_since_request=6.5,
        hours_since_promotion=6.5,
        travel_time_min=10,
        resolution_time_min=20,
    )
    assert outcome.promoted is True
    assert outcome.severity is Severity.MEDIUM

    # One minute later, the clock has restarted: no further promotion.
    next_pass = escalate(
        severity=Severity.MEDIUM,
        hours_since_request=6.52,
        hours_since_promotion=0.02,
        travel_time_min=10,
        resolution_time_min=20,
    )
    assert next_pass.promoted is False
    assert next_pass.severity is Severity.MEDIUM


def test_full_promotion_ladder_takes_thirteen_hours():
    total = (
        PROMOTION_DELAY_HOURS[Severity.LOW]
        + PROMOTION_DELAY_HOURS[Severity.MEDIUM]
        + PROMOTION_DELAY_HOURS[Severity.HIGH]
    )
    assert total == 13.0
    assert next_severity(Severity.CRITICAL) is None


def test_escalation_is_idempotent():
    """Re-running a pass with the same inputs must not drift the key upward."""
    kwargs = dict(
        severity=Severity.HIGH,
        hours_since_request=4.5,
        hours_since_promotion=0.5,
        travel_time_min=8,
        resolution_time_min=25,
    )
    first = escalate(**kwargs)
    second = escalate(**kwargs)
    assert first.heap_key == second.heap_key
    assert first.stage == second.stage


def test_status_open_and_terminal():
    assert RequestStatus.QUEUED.is_open
    assert RequestStatus.ASSIGNED.is_open
    assert RequestStatus.RESOLVED.is_terminal
    assert RequestStatus.CANCELLED.is_terminal
