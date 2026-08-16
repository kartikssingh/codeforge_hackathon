"""Request registry and lifecycle.

One store, one lock, one heap.  The previous build kept a ``RequestStore`` *and*
a ``PriorityQueue`` that each held the same dicts, so a request could be pushed
onto the heap twice (approve it while no volunteer was free, approve it again,
two entries) and "resolved" requests were never evicted from the ordering.

Lifecycle::

    AWAITING_REVIEW ──approve──▶ QUEUED ──dispatch──▶ ASSIGNED ──return──▶ RESOLVED
           │                        │                     │
           ├──override──▶ SUPERSEDED│                     │
           └──cancel───▶ CANCELLED ◀┴─────────cancel──────┘

Only ``QUEUED`` requests live in the heap; ``AWAITING_REVIEW`` ones still
escalate (an unreviewed report gets more urgent while it waits) but cannot be
dispatched until a human confirms them.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Iterable, Optional, Sequence

from aria.config import settings
from aria.core.errors import ConflictError, NotFoundError, ValidationError
from aria.core.eventbus import (
    EVENT_QUEUE_CHANGED,
    EVENT_REQUEST_CREATED,
    EVENT_REQUEST_UPDATED,
    EventBus,
    event_bus,
)
from aria.core.logging import audit, get_logger
from aria.core.priority_queue import IndexedPriorityQueue
from aria.domain.enums import RequestStatus, Severity
from aria.domain.priority import compute_heap_key
from aria.schemas import (
    EmergencyRequest,
    HandoffLog,
    ItemMovement,
    MaterialSelection,
    OverrideRequest,
    Situation,
)
from aria.services.inventory import InventoryService, ReservationResult
from aria.utils.timeutil import now

log = get_logger("requests")


def new_request_id() -> str:
    return f"REQ-{uuid.uuid4().hex[:6].upper()}"


class RequestService:
    """Owns every :class:`EmergencyRequest` and their queue ordering."""

    def __init__(
        self,
        inventory: InventoryService,
        *,
        bus: Optional[EventBus] = None,
    ) -> None:
        self._inventory = inventory
        self._bus = bus if bus is not None else event_bus
        self._requests: dict[str, EmergencyRequest] = {}
        self._queue = IndexedPriorityQueue()
        self._lock = threading.RLock()

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get(self, request_id: str) -> EmergencyRequest:
        with self._lock:
            request = self._requests.get(request_id)
        if request is None:
            raise NotFoundError(f"Request {request_id} not found", request_id=request_id)
        return request

    def find(self, request_id: str) -> Optional[EmergencyRequest]:
        with self._lock:
            return self._requests.get(request_id)

    def list(
        self,
        *,
        status: Optional[Iterable[RequestStatus]] = None,
        limit: Optional[int] = None,
    ) -> list[EmergencyRequest]:
        wanted = set(status) if status else None
        with self._lock:
            items = [r for r in self._requests.values() if wanted is None or r.status in wanted]
        items.sort(key=lambda r: (-r.heap_key, r.created_at))
        return items[:limit] if limit else items

    def board(self) -> list[EmergencyRequest]:
        """Open requests, most urgent first — what the dashboard renders."""
        with self._lock:
            items = [r for r in self._requests.values() if r.status.is_open]
        items.sort(key=lambda r: (_status_rank(r.status), -r.heap_key, r.created_at))
        return items

    def history(self, limit: int = 50) -> list[EmergencyRequest]:
        with self._lock:
            items = [r for r in self._requests.values() if r.status.is_terminal]
        items.sort(key=lambda r: (r.resolved_at or r.created_at), reverse=True)
        return items[:limit]

    def queued_ids(self) -> list[str]:
        return self._queue.ordered_keys()

    def next_dispatchable(self) -> Optional[EmergencyRequest]:
        """Highest-priority QUEUED request, without removing it."""
        with self._lock:
            for request_id in self._queue.ordered_keys():
                request = self._requests.get(request_id)
                if request is not None and request.status == RequestStatus.QUEUED:
                    return request
                # Stale entry (cancelled behind our back) — drop it.
                self._queue.discard(request_id)
            return None

    def escalatable(self) -> list[EmergencyRequest]:
        with self._lock:
            return [
                r
                for r in self._requests.values()
                if r.status in {RequestStatus.AWAITING_REVIEW, RequestStatus.QUEUED}
            ]

    # ── Creation ──────────────────────────────────────────────────────────────

    def add(self, request: EmergencyRequest) -> EmergencyRequest:
        """Register a freshly triaged request (still awaiting human review)."""
        with self._lock:
            if request.request_id in self._requests:
                raise ConflictError(
                    f"Request {request.request_id} already exists",
                    request_id=request.request_id,
                )
            self._recompute_key(request)
            self._requests[request.request_id] = request
        log.info(
            "Registered %s (%s, %d situation(s))",
            request.request_id,
            request.severity.value,
            len(request.situations),
        )
        self._publish(EVENT_REQUEST_CREATED, request)
        return request

    # ── Transitions ───────────────────────────────────────────────────────────

    def approve(
        self,
        request_id: str,
        selected_indices: Sequence[int],
        *,
        material_overrides: Sequence[MaterialSelection] = (),
        note: str = "",
    ) -> tuple[EmergencyRequest, ReservationResult]:
        """Confirm one or more situations, reserve their materials, queue it."""
        request = self.get(request_id)
        with self._lock:
            if request.status != RequestStatus.AWAITING_REVIEW:
                raise ConflictError(
                    f"Request {request_id} is {request.status.value}, not awaiting review",
                    request_id=request_id,
                    status=request.status.value,
                )
            if not request.situations:
                raise ValidationError(
                    "Request has no situations to approve", request_id=request_id
                )

            indices = sorted({i for i in selected_indices if 0 <= i < len(request.situations)})
            if not indices:
                indices = [0]
            for index, situation in enumerate(request.situations):
                situation.selected = index in indices

            demand = self._material_demand(request, material_overrides)
            reservation = self._inventory.reserve_many(demand, request_id=request_id)

            request.items_taken = reservation.reserved_items
            request.approved_at = now()
            request.status = RequestStatus.QUEUED
            if note:
                request.notes.append(note)
            self._reannotate_materials(request)
            self._recompute_key(request)
            self._queue.upsert(
                request.request_id, request.heap_key, order=request.created_at.timestamp()
            )
            request.handoff_logs.append(
                HandoffLog(
                    step="approved",
                    from_agent="SHELTER_MANAGER",
                    to_agent="PRIORITY_QUEUE",
                    reason=f"{len(indices)} situation(s) confirmed",
                    detail={
                        "selected": indices,
                        "reserved": [m.model_dump() for m in request.items_taken],
                        "shortfalls": [s.to_dict() for s in reservation.shortfalls],
                    },
                )
            )

        audit.record(
            from_agent="shelter_manager",
            to_agent="priority_queue",
            reason="approved",
            request_id=request_id,
            selected=indices,
            severity=request.severity.value,
        )
        log.info(
            "Approved %s → QUEUED (key=%.1f, %d item(s) reserved)",
            request_id,
            request.heap_key,
            len(request.items_taken),
        )
        self._publish(EVENT_QUEUE_CHANGED, request)
        return request, reservation

    def create_override(
        self, source_request_id: str, payload: OverrideRequest
    ) -> tuple[EmergencyRequest, ReservationResult]:
        """Replace an AI assessment with the manager's own call.

        The source request is marked SUPERSEDED rather than deleted, so the audit
        trail keeps both what the model said and what the human decided.
        """
        source = self.get(source_request_id)
        if source.status not in {RequestStatus.AWAITING_REVIEW, RequestStatus.QUEUED}:
            raise ConflictError(
                f"Request {source_request_id} is {source.status.value} and cannot be overridden",
                request_id=source_request_id,
            )

        severity = payload.severity
        situation = Situation(
            label=payload.condition,
            severity=severity,
            confidence=1.0,
            travel_time_min=payload.travel_time_min,
            resolution_time_min=payload.resolution_time_min,
            materials=[
                {"item": resource.item, "quantity": resource.quantity}
                for resource in payload.resources
                if resource.quantity > 0
            ],
            instructions=payload.instructions
            or ([payload.notes] if payload.notes else ["Follow the manager's briefing on site."]),
            reasoning="Manual override by the shelter manager.",
            origin="manual",
            selected=True,
        )

        override = EmergencyRequest(
            request_id=new_request_id(),
            transcript=source.transcript,
            intake_mode="override",
            is_vague=False,
            situations=[situation],
            status=RequestStatus.AWAITING_REVIEW,
            notes=[f"Manual override of {source_request_id}"] + ([payload.notes] if payload.notes else []),
            handoff_logs=[
                HandoffLog(
                    step="manual_override",
                    from_agent="AI_TRIAGE",
                    to_agent="SHELTER_MANAGER",
                    reason=f"Superseded {source_request_id}",
                    detail={"condition": payload.condition, "severity": severity.value},
                )
            ],
        )

        with self._lock:
            self._annotate_materials(override)
            self._recompute_key(override)
            self._requests[override.request_id] = override

        # Release whatever the superseded request was holding, then approve the
        # override in the same flow so the manager only clicks once.
        self.cancel(source_request_id, reason=f"Superseded by {override.request_id}", status=RequestStatus.SUPERSEDED)
        approved, reservation = self.approve(override.request_id, [0])
        audit.record(
            from_agent="shelter_manager",
            to_agent="priority_queue",
            reason="manual override",
            request_id=approved.request_id,
            source_request_id=source_request_id,
        )
        return approved, reservation

    def cancel(
        self,
        request_id: str,
        *,
        reason: str = "Cancelled",
        status: RequestStatus = RequestStatus.CANCELLED,
    ) -> EmergencyRequest:
        """Withdraw a request and hand back anything it was holding."""
        request = self.get(request_id)
        with self._lock:
            if request.status.is_terminal:
                raise ConflictError(
                    f"Request {request_id} is already {request.status.value}",
                    request_id=request_id,
                )
            if request.status == RequestStatus.ASSIGNED:
                raise ConflictError(
                    "Bring the volunteer back to base before cancelling this request",
                    request_id=request_id,
                    volunteer_id=request.assigned_volunteer,
                )
            held = list(request.items_taken)
            request.items_taken = []
            request.status = status
            request.closed_reason = reason
            request.resolved_at = now()
            self._queue.discard(request_id)
            request.handoff_logs.append(
                HandoffLog(
                    step=status.value.lower(),
                    from_agent="SHELTER_MANAGER",
                    to_agent="SYSTEM",
                    reason=reason,
                )
            )
        if held:
            self._inventory.release_many(held, request_id=request_id)
        log.info("Request %s → %s (%s)", request_id, status.value, reason)
        audit.record(
            from_agent="shelter_manager",
            to_agent="system",
            reason=reason,
            request_id=request_id,
            status=status.value,
        )
        self._publish(EVENT_QUEUE_CHANGED, request)
        return request

    def mark_assigned(
        self,
        request_id: str,
        *,
        volunteer_id: str,
        assigned_at: Any,
        expected_return: Any,
    ) -> EmergencyRequest:
        request = self.get(request_id)
        with self._lock:
            request.status = RequestStatus.ASSIGNED
            request.assigned_volunteer = volunteer_id
            request.assigned_at = assigned_at
            request.expected_return = expected_return
            self._queue.discard(request_id)
            request.handoff_logs.append(
                HandoffLog(
                    step="dispatched",
                    from_agent="PRIORITY_QUEUE",
                    to_agent="DISPATCH_ENGINE",
                    reason=f"Assigned to {volunteer_id}",
                    detail={"expected_return": str(expected_return)},
                )
            )
        self._publish(EVENT_QUEUE_CHANGED, request)
        return request

    def mark_resolved(
        self,
        request_id: str,
        *,
        returned: Sequence[ItemMovement],
        consumed: Sequence[ItemMovement],
        note: str = "",
    ) -> EmergencyRequest:
        request = self.get(request_id)
        with self._lock:
            request.status = RequestStatus.RESOLVED
            request.actual_return = now()
            request.resolved_at = request.actual_return
            request.items_returned = list(returned)
            request.items_consumed = list(consumed)
            request.closed_reason = note or "Volunteer returned to base"
            self._queue.discard(request_id)
            request.handoff_logs.append(
                HandoffLog(
                    step="resolved",
                    from_agent="DISPATCH_ENGINE",
                    to_agent="SHELTER_MANAGER",
                    reason=request.closed_reason,
                    detail={
                        "returned": [m.model_dump() for m in returned],
                        "consumed": [m.model_dump() for m in consumed],
                    },
                )
            )
        log.info("Request %s resolved", request_id)
        self._publish(EVENT_QUEUE_CHANGED, request)
        return request

    def apply_escalation(
        self,
        request_id: str,
        *,
        severity: Severity,
        heap_key: float,
        stage: int,
        promoted: bool,
    ) -> Optional[EmergencyRequest]:
        """Write back one escalation result.  Returns the request if it changed."""
        with self._lock:
            request = self._requests.get(request_id)
            if request is None or request.status.is_terminal:
                return None
            unchanged = (
                request.severity == severity
                and abs(request.heap_key - heap_key) < 1e-9
                and request.escalation_stage == stage
            )
            if unchanged:
                return None

            request.severity = severity
            request.heap_key = heap_key
            request.escalation_stage = stage
            if promoted:
                request.promoted_at = now()
                for situation in request.selected_situations:
                    situation.severity = severity
                    situation.severity_score = severity.score
                request.handoff_logs.append(
                    HandoffLog(
                        step="escalated",
                        from_agent="ESCALATION_SCHEDULER",
                        to_agent="PRIORITY_QUEUE",
                        reason=f"Promoted to {severity.value} after waiting",
                        detail={"stage": stage, "heap_key": heap_key},
                    )
                )
            if request.status == RequestStatus.QUEUED:
                self._queue.upsert(request_id, heap_key, order=request.created_at.timestamp())
            return request

    # ── Internals ─────────────────────────────────────────────────────────────

    def _material_demand(
        self, request: EmergencyRequest, overrides: Sequence[MaterialSelection]
    ) -> list[tuple[str, int]]:
        """What the confirmed situations need, with the manager's edits applied.

        When two confirmed situations want the same item the demand is the
        **larger** of the two, not the sum: the situations are competing
        hypotheses about one casualty, so the volunteer carries enough for the
        worst case, not enough for both at once.
        """
        override_map = {m.item.strip().lower(): m.quantity for m in overrides}
        demand: dict[str, int] = {}
        for situation in request.situations:
            if not situation.selected:
                continue
            for material in situation.materials:
                key = material.item.strip()
                quantity = override_map.get(key.lower(), material.quantity)
                if quantity <= 0:
                    continue
                demand[key] = max(demand.get(key, 0), quantity)
        # A manager may also add an item no situation asked for.
        for item, quantity in override_map.items():
            if quantity > 0 and item not in {k.lower() for k in demand}:
                demand[item] = quantity
        return list(demand.items())

    def _annotate_materials(self, request: EmergencyRequest) -> None:
        for situation in request.situations:
            for material in situation.materials:
                info = self._inventory.availability(material.item, material.quantity)
                material.available = bool(info["available"])
                material.available_qty = int(info["available_qty"])
                material.bin = str(info["bin"])
                material.matched_item = info.get("matched_item")

    _reannotate_materials = _annotate_materials

    def _recompute_key(self, request: EmergencyRequest) -> None:
        """Derive the request's severity and heap key from its situations."""
        candidates = request.selected_situations or request.situations
        if not candidates:
            request.severity = Severity.HIGH
            request.heap_key = 0.0
            return
        for situation in candidates:
            situation.heap_key = compute_heap_key(
                situation.severity.score,
                situation.travel_time_min,
                situation.resolution_time_min,
                scale_factor=settings.queue.scale_factor,
            )
        dominant = max(candidates, key=lambda s: s.heap_key)
        request.severity = dominant.severity
        request.heap_key = dominant.heap_key

    def _publish(self, event_type: str, request: EmergencyRequest) -> None:
        if self._bus is None:
            return
        self._bus.publish(
            event_type,
            request_id=request.request_id,
            status=request.status.value,
            severity=request.severity.value,
        )
        if event_type != EVENT_REQUEST_UPDATED:
            self._bus.publish(EVENT_REQUEST_UPDATED, request_id=request.request_id)

    # ── Snapshot support ──────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": [r.model_dump(mode="json") for r in self._requests.values()],
            }

    def restore_snapshot(self, data: dict[str, Any]) -> int:
        restored = 0
        with self._lock:
            for payload in data.get("requests") or []:
                try:
                    request = EmergencyRequest.model_validate(payload)
                except Exception as exc:  # noqa: BLE001 - skip unreadable entries
                    log.warning("Skipping unreadable persisted request: %s", exc)
                    continue
                self._requests[request.request_id] = request
                if request.status == RequestStatus.QUEUED:
                    self._queue.upsert(
                        request.request_id,
                        request.heap_key,
                        order=request.created_at.timestamp(),
                    )
                restored += 1
        return restored


def _status_rank(status: RequestStatus) -> int:
    """Board ordering: things needing a decision surface above running work."""
    return {
        RequestStatus.AWAITING_REVIEW: 0,
        RequestStatus.QUEUED: 1,
        RequestStatus.ASSIGNED: 2,
    }.get(status, 3)
