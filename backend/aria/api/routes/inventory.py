"""Inventory endpoints.

Every response carries the full board so the stock panel, the queue and the
material availability shown on each situation card can never drift apart.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from aria.api.deps import hub as hub_dep
from aria.schemas import (
    ActionResponse,
    InventoryCreateRequest,
    InventoryRefillRequest,
    InventoryUpdateRequest,
)
from aria.services.hub import Hub

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("")
async def get_inventory(hub: Hub = Depends(hub_dep)) -> dict[str, Any]:
    return {
        "inventory": hub.inventory.all(),
        "buffer": hub.inventory.buffer(),
        "stats": hub.inventory.stats(),
    }


@router.get("/low")
async def get_low_stock(hub: Hub = Depends(hub_dep)) -> dict[str, Any]:
    """Items at or below the low-stock threshold, out, or fully reserved."""
    return {"low_stock": hub.inventory.low_stock()}


@router.get("/buffer")
async def get_buffer(hub: Hub = Depends(hub_dep)) -> dict[str, Any]:
    """Overflow store — returned stock that no longer fits its bin."""
    return {"buffer": hub.inventory.buffer()}


@router.get("/history")
async def get_history(
    limit: int = Query(default=100, ge=1, le=500), hub: Hub = Depends(hub_dep)
) -> dict[str, Any]:
    """Every stock movement: reserve, release, consume, restore, refill."""
    return {"history": hub.inventory.history(limit=limit)}


@router.post("", response_model=ActionResponse)
async def create_item(
    body: InventoryCreateRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    row = hub.inventory.create_item(
        body.item, body.capacity, bin_location=body.bin, category=body.category
    )
    hub.persistence.mark_dirty()
    return ActionResponse(board=hub.board(), detail={"item": row.model_dump()})


@router.post("/refill", response_model=ActionResponse)
async def refill(body: InventoryRefillRequest, hub: Hub = Depends(hub_dep)) -> ActionResponse:
    """``daily`` resets everything to capacity; ``partial`` tops up what is low."""
    if body.mode == "daily":
        count = hub.inventory.daily_refill()
    else:
        count = hub.inventory.partial_refill()
    hub.persistence.mark_dirty()
    return ActionResponse(board=hub.board(), detail={"mode": body.mode, "items_refilled": count})


@router.post("/{item}/stock", response_model=ActionResponse)
async def add_stock(
    item: str, body: InventoryUpdateRequest, hub: Hub = Depends(hub_dep)
) -> ActionResponse:
    """Add units to an existing item, up to its capacity."""
    row = hub.inventory.add_stock(item, body.quantity)
    hub.persistence.mark_dirty()
    return ActionResponse(board=hub.board(), detail={"item": row.model_dump()})


@router.delete("/{item}", response_model=ActionResponse)
async def delete_item(item: str, hub: Hub = Depends(hub_dep)) -> ActionResponse:
    """Remove an item.  Refused while any units are still reserved."""
    hub.inventory.delete_item(item)
    hub.persistence.mark_dirty()
    return ActionResponse(board=hub.board(), detail={"removed": item})
