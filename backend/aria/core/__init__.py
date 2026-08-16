"""Infrastructure primitives: errors, logging, the heap, the event bus."""

from __future__ import annotations

from aria.core.errors import (
    AgentUnavailableError,
    AriaError,
    ConflictError,
    InsufficientStockError,
    NotFoundError,
    PipelineError,
    ValidationError,
)
from aria.core.eventbus import (
    EVENT_ESCALATED,
    EVENT_INVENTORY_CHANGED,
    EVENT_QUEUE_CHANGED,
    EVENT_REQUEST_CREATED,
    EVENT_REQUEST_UPDATED,
    EVENT_VOLUNTEERS_CHANGED,
    Event,
    EventBus,
    Subscription,
    event_bus,
)
from aria.core.logging import AuditTrail, audit, get_logger, setup_logging
from aria.core.priority_queue import IndexedPriorityQueue

__all__ = [
    "EVENT_ESCALATED",
    "EVENT_INVENTORY_CHANGED",
    "EVENT_QUEUE_CHANGED",
    "EVENT_REQUEST_CREATED",
    "EVENT_REQUEST_UPDATED",
    "EVENT_VOLUNTEERS_CHANGED",
    "AgentUnavailableError",
    "AriaError",
    "AuditTrail",
    "ConflictError",
    "Event",
    "EventBus",
    "IndexedPriorityQueue",
    "InsufficientStockError",
    "NotFoundError",
    "PipelineError",
    "Subscription",
    "ValidationError",
    "audit",
    "event_bus",
    "get_logger",
    "setup_logging",
]
