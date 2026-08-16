"""Volunteer roster and dispatch engine.

Volunteers are the scarcest resource in a shelter, so the rules are deliberately
conservative:

* A volunteer is only ever assigned to the single highest-priority QUEUED
  request — never to "the next one in the list".
* The countdown to ``expected_return`` is advisory.  When it hits zero nothing
  happens automatically: the volunteer stays BUSY until the shelter head clicks
  *Back at base*.  Auto-freeing someone who has not actually returned would send
  a second person to a second incident while the first is still out.
* ``OFF_DUTY`` exists so a volunteer can be rested (see OPS-07) without being
  deleted and losing their mission history.
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Sequence

from aria.config import settings
from aria.core.errors import ConflictError, NotFoundError, ValidationError
from aria.core.eventbus import EVENT_VOLUNTEERS_CHANGED, EventBus, event_bus
from aria.core.logging import audit, get_logger
from aria.domain.enums import RequestStatus, VolunteerStatus
from aria.schemas import EmergencyRequest, ItemMovement, Volunteer
from aria.services.inventory import InventoryService
from aria.services.requests import RequestService
from aria.utils.timeutil import format_clock, now, plus_minutes

log = get_logger("dispatch")


class DispatchService:
    def __init__(
        self,
        requests: RequestService,
        inventory: InventoryService,
        *,
        bus: Optional[EventBus] = None,
    ) -> None:
        self._requests = requests
        self._inventory = inventory
        self._bus = bus if bus is not None else event_bus
        self._volunteers: dict[str, Volunteer] = {}
        self._next_index = 1
        self._lock = threading.RLock()
        self.ensure_count(settings.dispatch.volunteer_count)

    # ── Roster ────────────────────────────────────────────────────────────────

    def all(self) -> list[Volunteer]:
        with self._lock:
            return sorted(self._volunteers.values(), key=lambda v: v.volunteer_id)

    def get(self, volunteer_id: str) -> Volunteer:
        with self._lock:
            volunteer = self._volunteers.get(volunteer_id)
        if volunteer is None:
            raise NotFoundError(f"Volunteer {volunteer_id} not found", volunteer_id=volunteer_id)
        return volunteer

    def add(self, name: str = "") -> Volunteer:
        with self._lock:
            if len(self._volunteers) >= settings.dispatch.max_volunteers:
                raise ValidationError(
                    f"Roster is capped at {settings.dispatch.max_volunteers} volunteers"
                )
            volunteer_id = f"V-{self._next_index:02d}"
            self._next_index += 1
            volunteer = Volunteer(volunteer_id=volunteer_id, name=name.strip() or volunteer_id)
            self._volunteers[volunteer_id] = volunteer
        log.info("Volunteer %s joined the roster", volunteer_id)
        self._notify()
        return volunteer

    def remove(self, volunteer_id: str) -> None:
        with self._lock:
            volunteer = self._volunteers.get(volunteer_id)
            if volunteer is None:
                raise NotFoundError(f"Volunteer {volunteer_id} not found", volunteer_id=volunteer_id)
            if volunteer.status == VolunteerStatus.BUSY:
                raise ConflictError(
                    f"{volunteer_id} is out on {volunteer.request_id} — bring them back first",
                    volunteer_id=volunteer_id,
                )
            del self._volunteers[volunteer_id]
        log.info("Volunteer %s left the roster", volunteer_id)
        self._notify()

    def set_status(self, volunteer_id: str, status: VolunteerStatus) -> Volunteer:
        volunteer = self.get(volunteer_id)
        with self._lock:
            if volunteer.status == VolunteerStatus.BUSY and status != VolunteerStatus.BUSY:
                raise ConflictError(
                    f"{volunteer_id} is on a mission — record their return first",
                    volunteer_id=volunteer_id,
                )
            if status == VolunteerStatus.BUSY:
                raise ValidationError("BUSY is set by the dispatcher, not by hand")
            volunteer.status = status
        self._notify()
        return volunteer

    def ensure_count(self, count: int) -> list[Volunteer]:
        """Resize the roster.  Busy volunteers are never removed."""
        if count < 0:
            raise ValidationError("Volunteer count cannot be negative")
        if count > settings.dispatch.max_volunteers:
            raise ValidationError(
                f"Volunteer count is capped at {settings.dispatch.max_volunteers}"
            )
        with self._lock:
            current = len(self._volunteers)
            if count > current:
                for _ in range(count - current):
                    volunteer_id = f"V-{self._next_index:02d}"
                    self._next_index += 1
                    self._volunteers[volunteer_id] = Volunteer(
                        volunteer_id=volunteer_id, name=volunteer_id
                    )
            elif count < current:
                removable = [
                    v.volunteer_id
                    for v in sorted(self._volunteers.values(), key=lambda v: v.volunteer_id, reverse=True)
                    if v.status != VolunteerStatus.BUSY
                ]
                for volunteer_id in removable:
                    if len(self._volunteers) <= count:
                        break
                    del self._volunteers[volunteer_id]
            result = sorted(self._volunteers.values(), key=lambda v: v.volunteer_id)
        log.info("Roster resized to %d volunteer(s)", len(result))
        self._notify()
        return result

    def free_volunteer(self) -> Optional[Volunteer]:
        with self._lock:
            for volunteer in sorted(self._volunteers.values(), key=lambda v: v.volunteer_id):
                if volunteer.is_free:
                    return volunteer
        return None

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def dispatch_next(self) -> Optional[dict[str, str]]:
        """Assign the top QUEUED request to the first free volunteer."""
        with self._lock:
            volunteer = self.free_volunteer()
            if volunteer is None:
                return None
            request = self._requests.next_dispatchable()
            if request is None:
                return None

            primary = request.primary
            travel = primary.travel_time_min if primary else settings.triage.default_travel_time_min
            resolution = (
                primary.resolution_time_min if primary else settings.triage.default_resolution_time_min
            )
            assigned_at = now()
            expected_return = plus_minutes(assigned_at, travel + resolution)

            volunteer.status = VolunteerStatus.BUSY
            volunteer.request_id = request.request_id
            volunteer.request_summary = request.summary
            volunteer.assigned_at = assigned_at
            volunteer.expected_return = expected_return
            volunteer.items_taken = list(request.items_taken)

        self._requests.mark_assigned(
            request.request_id,
            volunteer_id=volunteer.volunteer_id,
            assigned_at=assigned_at,
            expected_return=expected_return,
        )
        log.info(
            "Dispatched %s → %s (back by %s)",
            request.request_id,
            volunteer.volunteer_id,
            format_clock(expected_return),
        )
        audit.record(
            from_agent="priority_queue",
            to_agent="dispatch_engine",
            reason="volunteer assigned",
            request_id=request.request_id,
            volunteer_id=volunteer.volunteer_id,
            severity=request.severity.value,
        )
        self._notify()
        return {"volunteer_id": volunteer.volunteer_id, "request_id": request.request_id}

    def dispatch_all(self) -> list[dict[str, str]]:
        """Keep assigning until volunteers or work run out."""
        if not settings.dispatch.auto_dispatch:
            return []
        assignments: list[dict[str, str]] = []
        while True:
            assignment = self.dispatch_next()
            if assignment is None:
                break
            assignments.append(assignment)
        return assignments

    def record_return(
        self,
        volunteer_id: str,
        returned_items: Sequence[ItemMovement],
        *,
        note: str = "",
    ) -> dict[str, Any]:
        """*Back at base*: settle stock, close the request, free the volunteer."""
        volunteer = self.get(volunteer_id)
        if volunteer.status != VolunteerStatus.BUSY:
            raise ConflictError(
                f"{volunteer_id} is not currently on a mission", volunteer_id=volunteer_id
            )

        request_id = volunteer.request_id
        taken = list(volunteer.items_taken)
        settlement = self._inventory.settle_return(taken, returned_items, request_id=request_id)

        request: Optional[EmergencyRequest] = None
        if request_id:
            existing = self._requests.find(request_id)
            if existing is not None and existing.status == RequestStatus.ASSIGNED:
                request = self._requests.mark_resolved(
                    request_id,
                    returned=[ItemMovement(**m) for m in settlement["restored"]],
                    consumed=[ItemMovement(**m) for m in settlement["consumed"]],
                    note=note,
                )

        with self._lock:
            volunteer.status = VolunteerStatus.AVAILABLE
            volunteer.request_id = None
            volunteer.request_summary = None
            volunteer.assigned_at = None
            volunteer.expected_return = None
            volunteer.items_taken = []
            volunteer.missions_completed += 1

        log.info("%s back at base (mission %s)", volunteer_id, request_id or "n/a")
        audit.record(
            from_agent="dispatch_engine",
            to_agent="inventory",
            reason="volunteer returned",
            request_id=request_id,
            volunteer_id=volunteer_id,
            **settlement,
        )
        self._notify()
        assignments = self.dispatch_all()
        return {
            "volunteer_id": volunteer_id,
            "request_id": request_id,
            "settlement": settlement,
            "request": request,
            "new_assignments": assignments,
        }

    # ── Stats & snapshots ─────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._lock:
            volunteers = list(self._volunteers.values())
        busy = sum(1 for v in volunteers if v.status == VolunteerStatus.BUSY)
        available = sum(1 for v in volunteers if v.status == VolunteerStatus.AVAILABLE)
        overdue = sum(
            1
            for v in volunteers
            if v.expected_return is not None and v.expected_return < now()
        )
        return {
            "total": len(volunteers),
            "busy": busy,
            "available": available,
            "off_duty": len(volunteers) - busy - available,
            "overdue": overdue,
            "missions_completed": sum(v.missions_completed for v in volunteers),
            "utilisation_pct": round(busy / len(volunteers) * 100) if volunteers else 0,
        }

    def _notify(self) -> None:
        if self._bus is not None:
            self._bus.publish(EVENT_VOLUNTEERS_CHANGED)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "next_index": self._next_index,
                "volunteers": [v.model_dump(mode="json") for v in self._volunteers.values()],
            }

    def restore_snapshot(self, data: dict[str, Any]) -> int:
        restored = 0
        with self._lock:
            volunteers = data.get("volunteers") or []
            if volunteers:
                self._volunteers.clear()
            for payload in volunteers:
                try:
                    volunteer = Volunteer.model_validate(payload)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Skipping unreadable persisted volunteer: %s", exc)
                    continue
                self._volunteers[volunteer.volunteer_id] = volunteer
                restored += 1
            self._next_index = max(
                int(data.get("next_index") or 1),
                max(
                    (_index_of(v) for v in self._volunteers),
                    default=0,
                )
                + 1,
            )
        return restored


def _index_of(volunteer_id: str) -> int:
    try:
        return int(volunteer_id.split("-", 1)[1])
    except (IndexError, ValueError):
        return 0
