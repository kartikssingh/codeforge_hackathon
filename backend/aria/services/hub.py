"""Composition root.

The hub owns every service, wires them together and hosts the few flows that
cross service boundaries — approving a request and then dispatching it, or
recording a return and then re-dispatching the freed volunteer.

Keeping those flows here rather than inside the individual services is what lets
``RequestService`` stay unaware of volunteers and ``DispatchService`` stay
unaware of HTTP.  The API layer talks only to the hub.
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Sequence

from aria.agents import retrieval
from aria.agents.pipeline import IntakePipeline, IntakeResult
from aria.agents.rules import rule_engine
from aria.config import settings
from aria.core.eventbus import EVENT_QUEUE_CHANGED, EventBus, event_bus
from aria.core.logging import get_logger, setup_logging
from aria.domain.enums import VolunteerStatus
from aria.schemas import (
    ApproveRequest,
    BoardResponse,
    EmergencyRequest,
    ItemMovement,
    OverrideRequest,
    Volunteer,
)
from aria.services.dispatch import DispatchService
from aria.services.escalation import EscalationService
from aria.services.inventory import InventoryService
from aria.services.metrics import MetricsService
from aria.services.persistence import PersistenceService
from aria.services.requests import RequestService

log = get_logger("hub")


class Hub:
    """Everything the API needs, assembled once per process."""

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        inventory: Optional[InventoryService] = None,
    ) -> None:
        self.bus = bus if bus is not None else event_bus
        # Injectable so tests (and a second shelter on the same machine) can run
        # against their own ledger without touching data/inventory.csv.
        self.inventory = inventory if inventory is not None else InventoryService(bus=self.bus)
        self.requests = RequestService(self.inventory, bus=self.bus)
        self.dispatch = DispatchService(self.requests, self.inventory, bus=self.bus)
        self.escalation = EscalationService(self.requests, bus=self.bus)
        self.metrics = MetricsService(self.requests, self.dispatch, self.inventory)
        self.persistence = PersistenceService()
        self.pipeline = IntakePipeline(self.inventory, rules=rule_engine)
        self._started = False
        self._lock = threading.RLock()

        self.persistence.register("requests", self.requests)
        self.persistence.register("volunteers", self.dispatch)
        self.persistence.register("inventory", self.inventory)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Restore state, build the index, start background workers."""
        with self._lock:
            if self._started:
                return
            self._started = True

        restored = self.persistence.load()
        if restored.get("requests"):
            log.info("Restored %d request(s) from the previous session", restored["requests"])

        # Building the index is the slowest part of boot; it is safe to fail.
        retrieval.build_index()

        self.escalation.start()
        self.persistence.start()
        # A restored board may already have queued work and free volunteers.
        self.dispatch.dispatch_all()
        log.info("ARIA hub ready")

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        self.escalation.stop()
        self.persistence.stop()
        log.info("ARIA hub stopped")

    # ── Intake ────────────────────────────────────────────────────────────────

    def intake_audio(
        self, audio_b64: str, *, filename: Optional[str] = None, npu_mode: bool = False
    ) -> IntakeResult:
        result = self.pipeline.from_audio(audio_b64, filename=filename, npu_mode=npu_mode)
        self.requests.add(result.request)
        self._touch()
        return result

    def intake_text(self, text: str, *, npu_mode: bool = False) -> IntakeResult:
        result = self.pipeline.from_text(text, npu_mode=npu_mode)
        self.requests.add(result.request)
        self._touch()
        return result

    # ── Request flows ─────────────────────────────────────────────────────────

    def approve(self, request_id: str, body: ApproveRequest) -> dict[str, Any]:
        request, reservation = self.requests.approve(
            request_id,
            body.selected_indices,
            material_overrides=body.material_overrides,
            note=body.note,
        )
        assignments = self.dispatch.dispatch_all()
        self._touch()
        return {
            "request": self.requests.get(request.request_id),
            "reservation": reservation.to_dict(),
            "assignments": assignments,
        }

    def override(self, request_id: str, body: OverrideRequest) -> dict[str, Any]:
        request, reservation = self.requests.create_override(request_id, body)
        assignments = self.dispatch.dispatch_all()
        self._touch()
        return {
            "request": self.requests.get(request.request_id),
            "source_request_id": request_id,
            "reservation": reservation.to_dict(),
            "assignments": assignments,
        }

    def cancel(self, request_id: str, reason: str) -> EmergencyRequest:
        request = self.requests.cancel(request_id, reason=reason)
        self.dispatch.dispatch_all()
        self._touch()
        return request

    # ── Volunteer flows ───────────────────────────────────────────────────────

    def volunteer_return(
        self, volunteer_id: str, returned_items: Sequence[ItemMovement], *, note: str = ""
    ) -> dict[str, Any]:
        outcome = self.dispatch.record_return(volunteer_id, returned_items, note=note)
        self._touch()
        return outcome

    def set_volunteer_count(self, count: int) -> list[Volunteer]:
        volunteers = self.dispatch.ensure_count(count)
        self.dispatch.dispatch_all()
        self._touch()
        return volunteers

    def add_volunteer(self, name: str = "") -> Volunteer:
        volunteer = self.dispatch.add(name)
        self.dispatch.dispatch_all()
        self._touch()
        return volunteer

    def remove_volunteer(self, volunteer_id: str) -> None:
        self.dispatch.remove(volunteer_id)
        self._touch()

    def set_volunteer_status(self, volunteer_id: str, status: VolunteerStatus) -> Volunteer:
        volunteer = self.dispatch.set_status(volunteer_id, status)
        if status == VolunteerStatus.AVAILABLE:
            self.dispatch.dispatch_all()
        self._touch()
        return volunteer

    # ── Board ─────────────────────────────────────────────────────────────────

    def board(self, *, include_metrics: bool = True) -> BoardResponse:
        """One consistent snapshot of everything the dashboard renders."""
        return BoardResponse(
            queue=self.requests.board(),
            volunteers=self.dispatch.all(),
            inventory=self.inventory.all(),
            buffer=self.inventory.buffer(),
            metrics=self.metrics.summary() if include_metrics else {},
        )

    def health(self) -> list[dict[str, Any]]:
        """Component-by-component diagnostics for ``/health/detail``."""
        from aria.agents import transcribe as transcribe_agent  # local: keeps boot light
        from aria.llm import describe as describe_llm

        whisper_ok, whisper_detail = transcribe_agent.is_available()
        index = retrieval.status()
        llm = describe_llm()

        return [
            {
                "name": "inventory",
                "ok": bool(self.inventory.rows()),
                "detail": f"{len(self.inventory.rows())} item(s) loaded",
            },
            {
                "name": "triage_rules",
                "ok": len(rule_engine) > 0,
                "detail": f"{len(rule_engine)} rule(s) loaded",
            },
            {
                "name": "protocol_index",
                "ok": bool(index["ready"]),
                "detail": index["error"] or f"{index['documents']} document(s) indexed",
            },
            {"name": "speech_to_text", "ok": whisper_ok, "detail": whisper_detail},
            {
                "name": "language_model",
                "ok": bool(llm.get("ok", llm.get("loaded"))),
                "detail": str(llm.get("detail", "")),
            },
            {
                "name": "escalation",
                "ok": self.escalation.running or not settings.queue.escalation_enabled,
                "detail": (
                    f"running every {settings.queue.escalation_interval_secs}s"
                    if self.escalation.running
                    else "disabled"
                ),
            },
            {
                "name": "persistence",
                "ok": True,
                "detail": (
                    f"snapshots → {settings.paths.state_file}"
                    if settings.persistence.enabled
                    else "disabled"
                ),
            },
        ]

    # ── Internals ─────────────────────────────────────────────────────────────

    def _touch(self) -> None:
        """Mark state dirty for the snapshot writer and nudge the dashboard."""
        self.persistence.mark_dirty()
        self.bus.publish(EVENT_QUEUE_CHANGED)

    # ── Test / admin helpers ──────────────────────────────────────────────────

    def reset(self) -> None:
        """Wipe live state (used by tests and ``POST /admin/reset``)."""
        for request in self.requests.list():
            if request.status.is_open:
                try:
                    self.requests.cancel(request.request_id, reason="Board reset")
                except Exception:  # noqa: BLE001 - assigned requests refuse; fine
                    continue
        self.persistence.clear()
        log.warning("Board reset by operator")


_hub: Optional[Hub] = None
_hub_lock = threading.Lock()


def get_hub() -> Hub:
    """Process-wide hub, created on first use."""
    global _hub
    with _hub_lock:
        if _hub is None:
            setup_logging()
            _hub = Hub()
        return _hub


def reset_hub() -> None:
    """Drop the singleton — used by tests to get a clean board."""
    global _hub
    with _hub_lock:
        if _hub is not None:
            _hub.stop()
        _hub = None
