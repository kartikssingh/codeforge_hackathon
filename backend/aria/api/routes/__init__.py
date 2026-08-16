"""HTTP routes.  Thin adapters: validate, call the hub, shape the response."""

from __future__ import annotations

from aria.api.routes import (
    admin,
    events,
    health,
    intake,
    inventory,
    metrics,
    requests,
    settings,
    volunteers,
)

#: Registration order — ``health`` first so it is reachable even if a later
#: router fails to import during development.
ALL_ROUTERS = (
    health.router,
    intake.router,
    requests.router,
    volunteers.router,
    inventory.router,
    metrics.router,
    events.router,
    settings.router,
    admin.router,
)

__all__ = ["ALL_ROUTERS"]
