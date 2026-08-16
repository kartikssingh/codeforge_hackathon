"""Request lifecycle endpoints — the human-in-the-loop surface.

Nothing is dispatched and no stock is committed until a person calls
``/approve``.  That is the design's central safety property: the models propose,
the shelter manager decides.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from aria.api.deps import hub as hub_dep
from aria.domain.enums import RequestStatus
from aria.schemas import (
    ActionResponse,
    ApproveRequest,
    BoardResponse,
    CancelRequest,
    EmergencyRequest,
    OverrideRequest,
)
from aria.services.hub import Hub

router = APIRouter(tags=["requests"])


@router.get("/board", response_model=BoardResponse)
async def get_board(
    metrics: bool = Query(default=True, description="Include the metrics block"),
    hub: Hub = Depends(hub_dep),
) -> BoardResponse:
    """The whole live board in one consistent snapshot."""
    return hub.board(include_metrics=metrics)


@router.get("/queue")
async def get_queue(hub: Hub = Depends(hub_dep)) -> dict[str, list[EmergencyRequest]]:
    """Open requests in priority order (highest heap key first)."""
    return {"queue": hub.requests.board()}


@router.get("/requests", response_model=list[EmergencyRequest])
async def list_requests(
    status: Optional[RequestStatus] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    hub: Hub = Depends(hub_dep),
) -> list[EmergencyRequest]:
    return hub.requests.list(status=[status] if status else None, limit=limit)


@router.get("/requests/history", response_model=list[EmergencyRequest])
async def request_history(
    limit: int = Query(default=50, ge=1, le=500),
    hub: Hub = Depends(hub_dep),
) -> list[EmergencyRequest]:
    """Closed requests, most recently resolved first."""
    return hub.requests.history(limit=limit)


@router.get("/requests/{request_id}", response_model=EmergencyRequest)
async def get_request(request_id: str, hub: Hub = Depends(hub_dep)) -> EmergencyRequest:
    return hub.requests.get(request_id)


@router.post("/requests/{request_id}/approve", response_model=ActionResponse)
async def approve_request(
    request_id: str, body: ApproveRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """Confirm situations, reserve their materials and queue for dispatch."""
    outcome = hub.approve(request_id, body)
    return ActionResponse(
        request=outcome["request"],
        board=hub.board(),
        detail={
            "reservation": outcome["reservation"],
            "assignments": outcome["assignments"],
        },
    )


@router.post("/requests/{request_id}/override", response_model=ActionResponse)
async def override_request(
    request_id: str, body: OverrideRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """Replace the AI assessment with the manager's own call.

    Creates a new request, marks the original SUPERSEDED and queues the override
    in one step — the original stays in the audit trail for review afterwards.
    """
    outcome = hub.override(request_id, body)
    return ActionResponse(
        request=outcome["request"],
        board=hub.board(),
        detail={
            "source_request_id": outcome["source_request_id"],
            "reservation": outcome["reservation"],
            "assignments": outcome["assignments"],
        },
    )


@router.post("/requests/{request_id}/cancel", response_model=ActionResponse)
async def cancel_request(
    request_id: str, body: CancelRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """Withdraw a request (false alarm, duplicate, handled elsewhere).

    Any stock it was holding goes straight back to available.
    """
    request = hub.cancel(request_id, body.reason)
    return ActionResponse(request=request, board=hub.board())
