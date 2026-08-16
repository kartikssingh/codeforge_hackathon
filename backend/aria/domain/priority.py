"""Priority key formula and time-based urgency escalation.

Two separate mechanisms keep the queue fair:

**1. The heap key** — a pure function of the request's current state::

    heap_key = severity_score × SCALE_FACTOR
               - (travel_time_min × 2)
               - resolution_time_min
               + escalation_boost

``SCALE_FACTOR`` (1000) makes the severity tier dominate: a CRITICAL case
outranks every HIGH case no matter how far away it is.  The time penalties only
order requests *inside* one tier — of two equally severe cases, the one that can
be reached and closed out faster goes first, so the shelter serves more people
per volunteer-hour.

**2. Escalation** — an unattended request gets more urgent over time, in two
different ways:

* a *boost* inside its own tier (schedule below), and
* a *promotion* to the next severity label once it has waited too long.

The promotion clock restarts on every promotion.  A LOW request therefore
reaches CRITICAL after 6 h + 4 h + 3 h = 13 h, not on the third scheduler tick.
Both functions are pure: they recompute the key from scratch rather than adding
to the previous value, so a restart, a replay, or a double tick can never make
the key drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from aria.domain.enums import Severity

# ── Escalation schedule ───────────────────────────────────────────────────────
# severity → [(hours_waited, key_boost, buffer_multiplier), ...] ascending.
# The highest tier whose threshold has been crossed wins (they do not stack);
# ``buffer = (travel + resolution) × buffer_multiplier`` is added on top so that
# a slow-to-reach case escalates faster than a quick one that has waited as long.
ESCALATION_SCHEDULE: dict[Severity, tuple[tuple[float, float, float], ...]] = {
    # A cardiac arrest is unsurvivable after minutes, not hours.
    Severity.CRITICAL: (
        (4 / 60, 500.0, 1.0),
        (10 / 60, 900.0, 1.5),
    ),
    Severity.HIGH: (
        (2.0, 20.0, 1.0),
        (4.0, 60.0, 1.2),
        (5.0, 150.0, 1.5),
    ),
    Severity.MEDIUM: (
        (4.0, 10.0, 1.0),
        (8.0, 30.0, 1.2),
        (12.0, 80.0, 1.5),
    ),
    Severity.LOW: (
        (6.0, 5.0, 1.0),
        (10.0, 15.0, 1.2),
        (13.0, 40.0, 1.5),
        (15.0, 100.0, 2.0),
    ),
}

# Hours a request may sit at a given severity before it is promoted one level.
PROMOTION_DELAY_HOURS: dict[Severity, float] = {
    Severity.LOW: 6.0,
    Severity.MEDIUM: 4.0,
    Severity.HIGH: 3.0,
}

_NEXT_SEVERITY: dict[Severity, Severity] = {
    Severity.LOW: Severity.MEDIUM,
    Severity.MEDIUM: Severity.HIGH,
    Severity.HIGH: Severity.CRITICAL,
}


def next_severity(severity: Severity) -> Severity | None:
    """The level *severity* is promoted to, or None if already CRITICAL."""
    return _NEXT_SEVERITY.get(severity)


def compute_heap_key(
    severity_score: int,
    travel_time_min: int,
    resolution_time_min: int,
    *,
    scale_factor: int = 1000,
    boost: float = 0.0,
) -> float:
    """Priority key — higher means served sooner.  See the module docstring."""
    return float(
        severity_score * scale_factor
        - (max(0, travel_time_min) * 2)
        - max(0, resolution_time_min)
        + boost
    )


def escalation_boost(
    severity: Severity,
    hours_waited: float,
    travel_time_min: int,
    resolution_time_min: int,
) -> tuple[float, int]:
    """Return ``(boost, stage)`` for a request waiting *hours_waited*.

    ``stage`` is the number of schedule tiers crossed — 0 means "not escalated
    yet" and is what the dashboard shows as the ⚠ marker threshold.
    """
    boost = 0.0
    stage = 0
    for threshold_hours, tier_boost, buffer_multiplier in ESCALATION_SCHEDULE.get(severity, ()):
        if hours_waited < threshold_hours:
            break
        buffer = (max(0, travel_time_min) + max(0, resolution_time_min)) * buffer_multiplier
        boost = tier_boost + buffer
        stage += 1
    return boost, stage


@dataclass(frozen=True)
class EscalationOutcome:
    """Result of re-evaluating one waiting request."""

    severity: Severity
    severity_score: int
    heap_key: float
    stage: int
    boost: float
    promoted: bool

    @property
    def changed_severity(self) -> bool:
        return self.promoted


def escalate(
    *,
    severity: Severity,
    hours_since_request: float,
    hours_since_promotion: float,
    travel_time_min: int,
    resolution_time_min: int,
    scale_factor: int = 1000,
) -> EscalationOutcome:
    """Recompute severity, boost and heap key for one waiting request.

    *hours_since_promotion* is measured from the last promotion, falling back to
    the request time when it has never been promoted.  At most one promotion is
    applied per call, which is why the scheduler interval must be far shorter
    than the promotion delays (60 s vs 3 h — comfortably true).
    """
    promoted = False
    delay = PROMOTION_DELAY_HOURS.get(severity)
    upgrade = next_severity(severity)
    if delay is not None and upgrade is not None and hours_since_promotion >= delay:
        severity = upgrade
        promoted = True

    boost, stage = escalation_boost(
        severity, hours_since_request, travel_time_min, resolution_time_min
    )
    key = compute_heap_key(
        severity.score,
        travel_time_min,
        resolution_time_min,
        scale_factor=scale_factor,
        boost=boost,
    )
    return EscalationOutcome(
        severity=severity,
        severity_score=severity.score,
        heap_key=key,
        stage=stage,
        boost=boost,
        promoted=promoted,
    )
