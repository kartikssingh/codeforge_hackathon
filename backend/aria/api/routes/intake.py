"""Intake endpoints — audio and typed reports.

Both run the same agent chain and return the same shape.  The heavy lifting
(denoise, Whisper, embeddings, LLM) is synchronous and CPU-bound, so it runs in
a worker thread: doing it inline would block the event loop and freeze the
health endpoint, the queue polling and the SSE stream for the whole 10-25 s of a
pipeline run.

A semaphore keeps concurrent runs to ``ARIA_MAX_CONCURRENT_PIPELINES`` (1 by
default) — two CPU inference jobs on a shelter laptop finish slower together
than one after the other.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from aria.agents.pipeline import IntakeResult
from aria.api.deps import hub as hub_dep, pipeline_semaphore
from aria.core.errors import AgentUnavailableError
from aria.core.logging import get_logger
from aria.schemas import AudioIntakeRequest, IntakeResponse, TextIntakeRequest
from aria.services.hub import Hub

log = get_logger("api.intake")

router = APIRouter(prefix="/pipeline", tags=["intake"])


def _to_response(result: IntakeResult) -> IntakeResponse:
    return IntakeResponse(
        request=result.request,
        timings_ms=result.timings_ms,
        degraded=result.degraded,
        notes=result.notes,
    )


@router.post("", response_model=IntakeResponse)
async def intake_audio(
    body: AudioIntakeRequest,
    hub: Hub = Depends(hub_dep),
    semaphore: asyncio.Semaphore = Depends(pipeline_semaphore),
) -> IntakeResponse:
    """Run a base64 audio report through denoise → transcribe → triage."""
    async with semaphore:
        try:
            result = await run_in_threadpool(
                hub.intake_audio,
                body.audio_b64,
                filename=body.filename,
                npu_mode=body.npu_mode,
            )
        except AgentUnavailableError as exc:
            # Speech-to-text is the one stage with no offline substitute.
            log.warning("Audio intake unavailable: %s", exc.message)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                    "hint": "Type the report into POST /pipeline/text instead.",
                },
            ) from exc
    return _to_response(result)


@router.post("/text", response_model=IntakeResponse)
async def intake_text(
    body: TextIntakeRequest,
    hub: Hub = Depends(hub_dep),
    semaphore: asyncio.Semaphore = Depends(pipeline_semaphore),
) -> IntakeResponse:
    """Triage a typed report — radio traffic, a runner's note, a paper form.

    This path needs no audio stack at all, so the shelter keeps working when
    Whisper or ffmpeg are missing.
    """
    async with semaphore:
        result = await run_in_threadpool(hub.intake_text, body.text, npu_mode=body.npu_mode)
    return _to_response(result)
