"""Health and diagnostics.

``/health`` stays deliberately trivial — the Electron main process polls it every
second while the window is still hidden, so it must never touch a model, the
disk or a lock.

``/health/detail`` is the one a human reads: it says which capabilities are
actually present on this machine, which is how you find out that Whisper is
missing *before* an emergency call arrives rather than during one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aria import __version__, uptime_secs
from aria.api.deps import hub as hub_dep
from aria.schemas import ComponentHealth, HealthDetailResponse, HealthResponse
from aria.services.hub import Hub

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__, uptime_secs=uptime_secs())


@router.get("/health/detail", response_model=HealthDetailResponse)
async def health_detail(hub: Hub = Depends(hub_dep)) -> HealthDetailResponse:
    components = [ComponentHealth(**component) for component in hub.health()]
    # "degraded" is honest and actionable: the core loop works, some capability
    # is missing.  Only a broken inventory or rule catalogue counts as "down".
    essential = {"inventory", "triage_rules"}
    if all(component.ok for component in components):
        status = "ok"
    elif any(not c.ok for c in components if c.name in essential):
        status = "down"
    else:
        status = "degraded"
    return HealthDetailResponse(
        status=status,
        version=__version__,
        uptime_secs=uptime_secs(),
        components=components,
    )
