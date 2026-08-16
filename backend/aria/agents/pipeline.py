"""The intake pipeline — one call from raw report to a reviewable request.

    audio ──▶ denoise ──▶ transcribe ─┐
                                      ├─▶ retrieve ─(vague?)─▶ expand ─▶ triage ─▶ logistics ─▶ request
    typed report ─────────────────────┘

Every stage is timed and every hand-off is recorded, so the dashboard can show
*why* a decision was made and how long each agent took.

Degradation is a first-class outcome rather than an error path.  A missing
denoiser, an unbuilt index or an unreachable LLM each remove capability without
stopping the run; the resulting request is flagged ``degraded`` and carries
notes explaining exactly what was unavailable.  The only hard failure is audio
intake with no speech-to-text model, because there is genuinely nothing to
triage — and even then the error names the text endpoint as the way through.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from aria.agents import denoise as denoise_agent
from aria.agents import retrieval
from aria.agents import transcribe as transcribe_agent
from aria.agents.logistics import annotate_situations
from aria.agents.rules import RuleEngine, rule_engine as default_rule_engine
from aria.agents.triage import run_triage
from aria.agents.vagueness import resolve_and_retrieve
from aria.config import settings
from aria.core.errors import AgentUnavailableError
from aria.core.logging import audit, get_logger
from aria.llm import get_llm
from aria.schemas import EmergencyRequest, HandoffLog
from aria.services.inventory import InventoryService
from aria.services.requests import new_request_id
from aria.utils.audiofile import decode_upload, temp_audio
from aria.utils.textutil import truncate

log = get_logger("pipeline")


@dataclass
class IntakeResult:
    request: EmergencyRequest
    timings_ms: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.request.degraded


class _Stopwatch:
    """Records per-stage durations without littering the code with time math."""

    def __init__(self) -> None:
        self.timings: dict[str, int] = {}
        self._start = time.perf_counter()
        self._mark = self._start

    def lap(self, stage: str) -> int:
        nowish = time.perf_counter()
        elapsed = int((nowish - self._mark) * 1000)
        self.timings[stage] = elapsed
        self._mark = nowish
        return elapsed

    def total(self) -> int:
        total = int((time.perf_counter() - self._start) * 1000)
        self.timings["total"] = total
        return total


class IntakePipeline:
    """Runs the agent chain.  Stateless apart from its service dependencies."""

    def __init__(
        self,
        inventory: InventoryService,
        *,
        rules: Optional[RuleEngine] = None,
    ) -> None:
        self._inventory = inventory
        self._rules = rules or default_rule_engine

    # ── Public entry points ───────────────────────────────────────────────────

    def from_audio(
        self,
        audio_b64: str,
        *,
        filename: Optional[str] = None,
        npu_mode: bool = False,
    ) -> IntakeResult:
        request_id = new_request_id()
        watch = _Stopwatch()
        notes: list[str] = []
        logs: list[HandoffLog] = []
        degraded = False

        data = decode_upload(audio_b64)
        watch.lap("decode")
        log.info(
            "[%s] Audio intake: %s (%.1f KB)", request_id, filename or "upload", len(data) / 1024
        )

        with temp_audio(data, request_id) as (raw_path, clean_path):
            audit.record(
                from_agent="audio_input",
                to_agent="denoiser",
                reason="new audio report",
                request_id=request_id,
                filename=filename or "",
                bytes=len(data),
            )
            result = denoise_agent.denoise(raw_path, clean_path)
            elapsed = watch.lap("denoise")
            logs.append(
                HandoffLog(
                    step="denoise",
                    from_agent="AUDIO_INPUT",
                    to_agent="DENOISER",
                    reason=(
                        f"cleaned with {result.backend}"
                        if result.applied
                        else f"skipped ({result.note})"
                    ),
                    duration_ms=elapsed,
                )
            )
            if not result.applied:
                degraded = True
                notes.append(f"Denoising skipped: {result.note}")

            try:
                transcript = transcribe_agent.transcribe(result.path)
            except AgentUnavailableError:
                watch.lap("transcribe")
                raise
            elapsed = watch.lap("transcribe")

        logs.append(
            HandoffLog(
                step="transcribe",
                from_agent="DENOISER",
                to_agent="INTAKE_AGENT",
                reason=f"transcribed {len(transcript)} characters",
                duration_ms=elapsed,
                detail={"transcript": truncate(transcript, 200)},
            )
        )
        log.info("[%s] Transcript: %s", request_id, truncate(transcript, 120))

        return self._triage(
            request_id=request_id,
            transcript=transcript,
            intake_mode="audio",
            npu_mode=npu_mode,
            watch=watch,
            logs=logs,
            notes=notes,
            degraded=degraded,
        )

    def from_text(self, text: str, *, npu_mode: bool = False) -> IntakeResult:
        """Typed intake — radio traffic, a runner's message, a paper form.

        Also the path that keeps the shelter running when Whisper or ffmpeg are
        not installed, and the one integration tests use.
        """
        request_id = new_request_id()
        watch = _Stopwatch()
        log.info("[%s] Text intake: %s", request_id, truncate(text, 120))
        audit.record(
            from_agent="text_input",
            to_agent="retrieval_agent",
            reason="typed report",
            request_id=request_id,
        )
        return self._triage(
            request_id=request_id,
            transcript=text.strip(),
            intake_mode="text",
            npu_mode=npu_mode,
            watch=watch,
            logs=[
                HandoffLog(
                    step="intake",
                    from_agent="DISPATCHER",
                    to_agent="RETRIEVAL_AGENT",
                    reason="report typed by the dispatcher",
                )
            ],
            notes=[],
            degraded=False,
        )

    # ── Shared tail of the pipeline ───────────────────────────────────────────

    def _triage(
        self,
        *,
        request_id: str,
        transcript: str,
        intake_mode: str,
        npu_mode: bool,
        watch: _Stopwatch,
        logs: list[HandoffLog],
        notes: list[str],
        degraded: bool,
    ) -> IntakeResult:
        llm = get_llm(prefer_npu=npu_mode)

        # ── Retrieval ─────────────────────────────────────────────────────────
        retrieval_result = retrieval.retrieve(transcript)
        elapsed = watch.lap("retrieval")
        if not retrieval_result.available:
            degraded = True
            notes.append(f"Protocol search unavailable: {retrieval_result.note}")
        logs.append(
            HandoffLog(
                step="retrieval",
                from_agent="INTAKE_AGENT",
                to_agent="RETRIEVAL_AGENT",
                reason=(
                    f"{len(retrieval_result.chunks)} passage(s), "
                    f"top relevance {retrieval_result.top_score:.2f}"
                ),
                duration_ms=elapsed,
                detail={"sources": retrieval_result.sources[:5]},
            )
        )

        chunks = retrieval_result.chunks
        is_vague = retrieval_result.is_vague

        # ── Vagueness expansion ───────────────────────────────────────────────
        if is_vague:
            expansion = resolve_and_retrieve(
                transcript,
                llm,
                retrieval.retrieve,
                rules=self._rules,
                base_chunks=chunks,
            )
            elapsed = watch.lap("vagueness")
            chunks = expansion.chunks
            notes.append(
                "Report was ambiguous "
                f"(relevance {retrieval_result.top_score:.2f} < "
                f"{settings.rag.confidence_threshold:.2f}); expanded into "
                f"{len(expansion.flat_hypotheses)} hypotheses."
            )
            logs.append(
                HandoffLog(
                    step="vagueness",
                    from_agent="RETRIEVAL_AGENT",
                    to_agent="VAGUENESS_AGENT",
                    reason=f"low confidence — ran {expansion.queries_run} extra quer(ies)",
                    duration_ms=elapsed,
                    detail={"hypotheses": expansion.hypotheses},
                )
            )
            audit.record(
                from_agent="retrieval_agent",
                to_agent="vagueness_agent",
                reason=f"top score {retrieval_result.top_score:.2f} below threshold",
                request_id=request_id,
                hypotheses=expansion.flat_hypotheses,
            )

        # ── Triage ────────────────────────────────────────────────────────────
        outcome = run_triage(transcript, chunks, llm, rules=self._rules)
        elapsed = watch.lap("triage")
        degraded = degraded or outcome.degraded
        notes.extend(outcome.notes)
        logs.append(
            HandoffLog(
                step="triage",
                from_agent="RETRIEVAL_AGENT",
                to_agent="TRIAGE_AGENT",
                reason=f"{len(outcome.situations)} situation(s) {outcome.origin_counts}",
                duration_ms=elapsed,
                detail={
                    "engines": {"llm": outcome.used_llm, "rules": outcome.used_rules},
                    "labels": [s.label for s in outcome.situations],
                },
            )
        )

        # ── Logistics ─────────────────────────────────────────────────────────
        logistics = annotate_situations(outcome.situations, self._inventory)
        elapsed = watch.lap("logistics")
        if logistics.missing:
            notes.append("Not stocked here: " + ", ".join(logistics.missing))
        if logistics.short:
            notes.append("Low or out of stock: " + ", ".join(logistics.short))
        logs.append(
            HandoffLog(
                step="logistics",
                from_agent="TRIAGE_AGENT",
                to_agent="LOGISTICS_AGENT",
                reason=(
                    "all materials in stock"
                    if logistics.all_available
                    else f"{len(logistics.missing)} unstocked, {len(logistics.short)} short"
                ),
                duration_ms=elapsed,
                detail={"missing": logistics.missing, "short": logistics.short},
            )
        )

        request = EmergencyRequest(
            request_id=request_id,
            transcript=transcript,
            intake_mode=intake_mode,
            is_vague=is_vague,
            retrieval_top_score=round(retrieval_result.top_score, 3),
            situations=outcome.situations,
            handoff_logs=logs,
            degraded=degraded,
            notes=notes,
        )
        total = watch.total()
        log.info(
            "[%s] Pipeline complete in %d ms — %s / %s",
            request_id,
            total,
            request.situations[0].severity.value if request.situations else "?",
            request.summary,
        )
        audit.record(
            from_agent="logistics_agent",
            to_agent="shelter_manager",
            reason="awaiting human review",
            request_id=request_id,
            duration_ms=total,
            situations=[s.label for s in request.situations],
            degraded=degraded,
        )
        return IntakeResult(request=request, timings_ms=watch.timings, notes=notes)
