"""FastAPI application factory.

``create_app()`` exists so tests can build an isolated instance instead of
importing a module-level singleton, and so the ASGI object is constructed the
same way whether it is started by ``python backend/main.py``, by uvicorn's
import string, or by Electron.

Start-up work (restoring state, building the vector index, starting the
escalation thread) happens in the lifespan handler — the modern replacement for
``@app.on_event("startup")``, which is deprecated and offers no shutdown
symmetry.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aria import __version__
from aria.api.routes import ALL_ROUTERS
from aria.config import settings
from aria.core.errors import AriaError
from aria.core.logging import get_logger, setup_logging
from aria.services.hub import get_hub

log = get_logger("api")

DESCRIPTION = """
Offline triage and volunteer dispatch for disaster relief shelters.

* `POST /pipeline` — audio distress report → ranked situations
* `POST /pipeline/text` — same, from typed text (works with no audio stack)
* `POST /requests/{id}/approve` — the human-in-the-loop gate
* `GET  /board` — one consistent snapshot of queue, roster and stock
* `GET  /events` — Server-Sent Events stream of every change

Bound to 127.0.0.1. No outbound network calls are made at runtime.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    log.info("ARIA %s starting on %s:%d", __version__, settings.api.host, settings.api.port)
    # Created here so it binds to the loop that will actually await it.
    app.state.pipeline_semaphore = asyncio.Semaphore(
        max(1, settings.api.max_concurrent_pipelines)
    )
    hub = get_hub()
    hub.start()
    try:
        yield
    finally:
        hub.stop()
        log.info("ARIA stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ARIA — Autonomous Relief Intelligence Agent",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )

    # The renderer talks to 127.0.0.1 only; CORS is here for browser-based
    # debugging of the same origin set, not to open the API to a network.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(file://|http://(localhost|127\.0\.0\.1)(:\d+)?)$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in ALL_ROUTERS:
        app.include_router(router)

    _install_error_handlers(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AriaError)
    async def handle_domain_error(_request: Request, exc: AriaError) -> JSONResponse:
        # Expected failures: wrong state, missing item, not enough stock.
        log.info("%s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request body did not validate.",
                    "detail": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # Anything reaching here is a bug; log the traceback but never leak it
        # to the UI, which shows the message verbatim in a toast.
        log.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong on the backend. Check backend/logs/aria.log.",
                    "detail": {},
                }
            },
        )


#: Module-level ASGI app for ``uvicorn aria.api:app``.
app = create_app()

__all__ = ["app", "create_app", "lifespan"]
