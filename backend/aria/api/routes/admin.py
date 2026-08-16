"""Maintenance endpoints.

Bound to 127.0.0.1 like the rest of the API, and intended for the operator
sitting at the shelter laptop: reload the protocol index after copying new PDFs
onto the machine, or clear the board between drills.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aria.agents import retrieval
from aria.agents.rules import rule_engine
from aria.api.deps import hub as hub_dep
from aria.core.logging import get_logger
from aria.schemas import ActionResponse
from aria.services.hub import Hub

log = get_logger("api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/reload")
async def reload_resources(
    rebuild_index: bool = False, hub: Hub = Depends(hub_dep)
) -> dict[str, Any]:
    """Re-read the inventory CSV, the triage rules and (optionally) the PDFs."""
    import aria.llm as llm_registry

    hub.inventory.load()
    rules = rule_engine.load()
    llm_registry.reset()
    index_ready = retrieval.build_index(force=rebuild_index)
    log.info("Resources reloaded (index_ready=%s, rules=%d)", index_ready, rules)
    return {
        "inventory_items": len(hub.inventory.rows()),
        "triage_rules": rules,
        "index_ready": index_ready,
        "llm_cache": "cleared",
    }


@router.post("/snapshot")
async def force_snapshot(hub: Hub = Depends(hub_dep)) -> dict[str, Any]:
    """Write the state snapshot immediately instead of waiting for the flusher."""
    written = hub.persistence.flush(force=True)
    return {"written": written}


@router.post("/reset", response_model=ActionResponse)
async def reset_board(hub: Hub = Depends(hub_dep)) -> ActionResponse:
    """Cancel every open request and delete the snapshot.

    Requests with a volunteer already out are left alone — bring them back to
    base first, so nobody is written out of the system while still in the field.
    """
    hub.reset()
    return ActionResponse(board=hub.board())
