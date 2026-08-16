"""Domain enumerations shared by the whole backend.

These are ``str`` enums so they serialise straight to JSON and compare equal to
plain strings — which keeps the wire format identical to the old dict-based
implementation while giving the Python side real type safety.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    """Triage severity, ordered CRITICAL > HIGH > MEDIUM > LOW."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def score(self) -> int:
        """Base severity score used by the heap key formula."""
        return _SEVERITY_SCORES[self]

    @property
    def rank(self) -> int:
        """0 = most urgent.  Handy for sorting without touching the score."""
        return _SEVERITY_RANKS[self]

    @classmethod
    def from_any(cls, value: object, default: "Severity" = None) -> "Severity":  # type: ignore[assignment]
        """Coerce arbitrary LLM / user input into a Severity.

        Accepts the enum itself, any casing of the label, and the numeric base
        scores (100/75/50/25).  Unknown input falls back to *default*, which is
        HIGH — deliberately pessimistic, because under-triaging is the failure
        mode that hurts people.
        """
        fallback = cls.HIGH if default is None else default
        if isinstance(value, cls):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return cls.from_score(int(value), fallback)
        if isinstance(value, str):
            token = value.strip().upper()
            if token in cls.__members__:
                return cls[token]
            if token.isdigit():
                return cls.from_score(int(token), fallback)
        return fallback

    @classmethod
    def from_score(cls, score: int, default: "Severity" = None) -> "Severity":  # type: ignore[assignment]
        """Map a numeric score onto the nearest severity band."""
        fallback = cls.HIGH if default is None else default
        if score >= 88:
            return cls.CRITICAL
        if score >= 63:
            return cls.HIGH
        if score >= 38:
            return cls.MEDIUM
        if score > 0:
            return cls.LOW
        return fallback


_SEVERITY_SCORES: dict[Severity, int] = {
    Severity.CRITICAL: 100,
    Severity.HIGH: 75,
    Severity.MEDIUM: 50,
    Severity.LOW: 25,
}

_SEVERITY_RANKS: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}


class RequestStatus(str, Enum):
    """Lifecycle of an emergency request.

    ``AWAITING_REVIEW`` → the pipeline produced situations, a human must pick.
    ``QUEUED``          → approved and sitting in the priority heap.
    ``ASSIGNED``        → a volunteer is en route / on site.
    ``RESOLVED``        → volunteer returned and the manager closed it out.
    ``CANCELLED``       → withdrawn before dispatch (duplicate, false alarm…).
    ``SUPERSEDED``      → replaced by a manual-override request.
    """

    AWAITING_REVIEW = "AWAITING_REVIEW"
    QUEUED = "QUEUED"
    ASSIGNED = "ASSIGNED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"

    @property
    def is_open(self) -> bool:
        """True while the request still needs someone to do something."""
        return self in _OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return not self.is_open


_OPEN_STATUSES = frozenset(
    {RequestStatus.AWAITING_REVIEW, RequestStatus.QUEUED, RequestStatus.ASSIGNED}
)


class VolunteerStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    OFF_DUTY = "OFF_DUTY"


class LlmBackend(str, Enum):
    OLLAMA = "ollama"
    NPU_ONNX = "onnx"
    NPU_OPENVINO = "openvino"
    NONE = "none"
