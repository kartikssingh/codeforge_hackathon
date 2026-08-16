"""FastAPI dependencies.

Routes depend on :func:`hub` rather than importing the singleton directly, so a
test can override the dependency and hand every route an isolated board.
"""

from __future__ import annotations

import asyncio

from fastapi import Request

from aria.config import settings
from aria.services.hub import Hub, get_hub


def hub() -> Hub:
    return get_hub()


async def pipeline_semaphore(request: Request) -> asyncio.Semaphore:
    """Serialises heavy pipeline runs.

    CPU inference on a shelter laptop cannot usefully run twice at once; without
    this, two uploads thrash and both take longer than they would in sequence.

    The semaphore is created by the lifespan handler and lives on ``app.state``,
    because an :class:`asyncio.Semaphore` binds to the loop it is first awaited
    on — a module-level one would break the moment a second event loop appeared
    (which is exactly what a test suite does).
    """
    existing = getattr(request.app.state, "pipeline_semaphore", None)
    if existing is None:
        existing = asyncio.Semaphore(max(1, settings.api.max_concurrent_pipelines))
        request.app.state.pipeline_semaphore = existing
    return existing
