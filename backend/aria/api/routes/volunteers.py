"""Volunteer roster and the return flow."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aria.api.deps import hub as hub_dep
from aria.schemas import (
    ActionResponse,
    Volunteer,
    VolunteerCountRequest,
    VolunteerCreateRequest,
    VolunteerReturnRequest,
    VolunteerStatusRequest,
)
from aria.services.hub import Hub

router = APIRouter(prefix="/volunteers", tags=["volunteers"])


@router.get("", response_model=list[Volunteer])
async def list_volunteers(hub: Hub = Depends(hub_dep)) -> list[Volunteer]:
    return hub.dispatch.all()


@router.post("", response_model=ActionResponse)
async def add_volunteer(
    body: VolunteerCreateRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """Add one named volunteer; they are considered for dispatch immediately."""
    volunteer = hub.add_volunteer(body.name)
    return ActionResponse(board=hub.board(), detail={"volunteer": volunteer.model_dump(mode="json")})


@router.post("/count", response_model=ActionResponse)
async def set_volunteer_count(
    body: VolunteerCountRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """Resize the roster.  Volunteers currently out on a mission are kept."""
    volunteers = hub.set_volunteer_count(body.count)
    return ActionResponse(board=hub.board(), detail={"count": len(volunteers)})


@router.patch("/{volunteer_id}", response_model=ActionResponse)
async def set_volunteer_status(
    volunteer_id: str, body: VolunteerStatusRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """Rest a volunteer (OFF_DUTY) or bring them back on shift."""
    volunteer = hub.set_volunteer_status(volunteer_id, body.status)
    return ActionResponse(board=hub.board(), detail={"volunteer": volunteer.model_dump(mode="json")})


@router.delete("/{volunteer_id}", response_model=ActionResponse)
async def remove_volunteer(volunteer_id: str, hub: Hub = Depends(hub_dep)) -> ActionResponse:
    hub.remove_volunteer(volunteer_id)
    return ActionResponse(board=hub.board(), detail={"removed": volunteer_id})


@router.post("/{volunteer_id}/return", response_model=ActionResponse)
async def volunteer_return(
    volunteer_id: str, body: VolunteerReturnRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """*Back at base*.

    Restores what came back, writes off what was used, closes the request and
    immediately considers the freed volunteer for the next task.
    """
    outcome = hub.volunteer_return(volunteer_id, body.returned_items, note=body.note)
    return ActionResponse(
        request=outcome.get("request"),
        board=hub.board(),
        detail={
            "volunteer_id": outcome["volunteer_id"],
            "settlement": outcome["settlement"],
            "new_assignments": outcome["new_assignments"],
        },
    )
