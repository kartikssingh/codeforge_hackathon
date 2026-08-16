"""Pure domain logic — enums, scoring, escalation policy, protocol rules.

Nothing in this package touches I/O, the network, or FastAPI.  It is the layer
that can be reasoned about (and unit-tested) without any dependency at all.
"""

from __future__ import annotations

from aria.domain.enums import RequestStatus, Severity, VolunteerStatus
from aria.domain.priority import (
    ESCALATION_SCHEDULE,
    PROMOTION_DELAY_HOURS,
    EscalationOutcome,
    compute_heap_key,
    escalate,
    next_severity,
)

__all__ = [
    "ESCALATION_SCHEDULE",
    "PROMOTION_DELAY_HOURS",
    "EscalationOutcome",
    "RequestStatus",
    "Severity",
    "VolunteerStatus",
    "compute_heap_key",
    "escalate",
    "next_severity",
]
