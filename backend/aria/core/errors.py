"""Domain exceptions.

Services raise these; a single handler in :mod:`aria.api` turns them into JSON
responses.  That keeps HTTP concerns out of the business logic and guarantees
every error the renderer sees has the same shape::

    {"error": {"code": "not_found", "message": "…", "detail": {...}}}
"""

from __future__ import annotations

from typing import Any


class AriaError(Exception):
    """Base class for every expected failure."""

    status_code: int = 400
    code: str = "error"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


class NotFoundError(AriaError):
    status_code = 404
    code = "not_found"


class ConflictError(AriaError):
    """The object exists but is in the wrong state for this transition."""

    status_code = 409
    code = "conflict"


class ValidationError(AriaError):
    status_code = 422
    code = "invalid_request"


class InsufficientStockError(ConflictError):
    code = "insufficient_stock"


class AgentUnavailableError(AriaError):
    """A model or external runtime the step needs is not installed/reachable.

    This is a *degraded mode* signal, not a bug: the pipeline catches it, records
    it on the request, and keeps going with whatever still works.
    """

    status_code = 503
    code = "agent_unavailable"


class PipelineError(AriaError):
    status_code = 500
    code = "pipeline_failed"
