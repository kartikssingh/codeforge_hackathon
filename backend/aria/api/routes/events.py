"""Server-Sent Events — live board updates.

The renderer opens one long-lived connection and receives an event whenever
anything changes, instead of three independent polls racing each other.  On a
quiet night this is close to zero CPU; on a busy one the dashboard updates the
instant a volunteer is dispatched rather than up to three seconds later.

Events published on worker threads land in a bounded per-subscriber queue; this
generator drains it on the event loop.  A heartbeat comment keeps the connection
alive through any proxy and lets the client notice a dead stream.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from aria.api.deps import hub as hub_dep
from aria.config import settings
from aria.core.eventbus import EVENT_HEARTBEAT, Event
from aria.core.logging import get_logger
from aria.services.hub import Hub

log = get_logger("api.events")

router = APIRouter(tags=["events"])

_POLL_INTERVAL = 0.25


def _frame(event: Event) -> str:
    return f"event: {event.type}\ndata: {json.dumps(event.to_dict(), default=str)}\n\n"


def _synthetic(event_type: str, **payload: object) -> str:
    """Same envelope as a bus event, for frames the stream generates itself."""
    return _frame(Event(type=event_type, payload=dict(payload)))


@router.get("/events")
async def stream_events(request: Request, hub: Hub = Depends(hub_dep)) -> StreamingResponse:
    """Subscribe to board changes as an SSE stream."""

    async def generator() -> AsyncIterator[str]:
        subscription = hub.bus.subscribe()
        log.info("SSE client connected (%d total)", hub.bus.subscriber_count)
        try:
            # Tell the client the stream is live so it can slow its poll timer.
            yield _synthetic("ready", subscribers=hub.bus.subscriber_count)
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    break
                events = subscription.drain()
                if events:
                    idle = 0.0
                    for event in events:
                        yield _frame(event)
                else:
                    await asyncio.sleep(_POLL_INTERVAL)
                    idle += _POLL_INTERVAL
                    if idle >= settings.api.sse_heartbeat_secs:
                        idle = 0.0
                        yield _synthetic(EVENT_HEARTBEAT, ok=True)
        except asyncio.CancelledError:  # pragma: no cover - client vanished
            raise
        finally:
            subscription.close()
            log.info("SSE client disconnected (%d left)", hub.bus.subscriber_count)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
