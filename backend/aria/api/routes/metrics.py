"""Operational metrics and the explainability log."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from aria.api.deps import hub as hub_dep
from aria.core.logging import audit
from aria.services.hub import Hub

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def get_metrics(hub: Hub = Depends(hub_dep)) -> dict[str, Any]:
    """Live counts, wait times, SLA breaches, roster and stock summaries."""
    return hub.metrics.summary()


@router.get("/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=500),
    request_id: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """Agent hand-off trail — newest first.

    This is the answer to "why did it decide that?": every step records which
    agent handed off to which, why, and how long it took.
    """
    return {"logs": audit.recent(limit=limit, request_id=request_id)}
