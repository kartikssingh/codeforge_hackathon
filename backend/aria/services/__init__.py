"""Application services — state, lifecycle and orchestration.

``hub`` is the composition root; everything else is a single-responsibility
service it owns.  Import the hub, not the individual services, from the API
layer::

    from aria.services.hub import get_hub
"""

from __future__ import annotations

from aria.services.dispatch import DispatchService
from aria.services.escalation import EscalationService
from aria.services.hub import Hub, get_hub, reset_hub
from aria.services.inventory import InventoryService
from aria.services.metrics import MetricsService
from aria.services.persistence import PersistenceService
from aria.services.requests import RequestService

__all__ = [
    "DispatchService",
    "EscalationService",
    "Hub",
    "InventoryService",
    "MetricsService",
    "PersistenceService",
    "RequestService",
    "get_hub",
    "reset_hub",
]
