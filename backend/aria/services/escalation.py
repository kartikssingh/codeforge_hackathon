"""Background urgency escalation.

Runs :func:`aria.domain.priority.escalate` over every waiting request on a fixed
interval so that nobody starves at the bottom of the queue.  A request that has
been waiting long enough is both boosted within its tier and eventually promoted
to the next severity label.

Implemented with a plain daemon thread instead of APScheduler: one less
dependency to install on an offline laptop, deterministic shutdown via an
``Event``, and :meth:`run_once` is directly callable from tests without any
scheduler machinery.
"""

from __future__ import annotations

import threading
from typing import Optional

from aria.config import settings
from aria.core.eventbus import EVENT_ESCALATED, EVENT_QUEUE_CHANGED, EventBus, event_bus
from aria.core.logging import audit, get_logger
from aria.domain.priority import escalate
from aria.services.requests import RequestService
from aria.utils.timeutil import hours_between, now

log = get_logger("escalation")


class EscalationService:
    def __init__(
        self,
        requests: RequestService,
        *,
        bus: Optional[EventBus] = None,
        interval_secs: Optional[int] = None,
    ) -> None:
        self._requests = requests
        self._bus = bus if bus is not None else event_bus
        self._interval = interval_secs or settings.queue.escalation_interval_secs
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not settings.queue.escalation_enabled:
            log.info("Escalation disabled by configuration")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="aria-escalation", daemon=True
        )
        self._thread.start()
        log.info("Escalation scheduler started (every %ds)", self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        log.info("Escalation scheduler stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Work ──────────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # Wait first so startup is not immediately followed by a no-op pass.
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the loop must outlive any single failure
                log.exception("Escalation pass failed")

    def run_once(self) -> int:
        """One escalation pass.  Returns how many requests changed."""
        reference = now()
        changed = 0
        promotions = 0

        for request in self._requests.escalatable():
            primary = request.primary
            travel = primary.travel_time_min if primary else settings.triage.default_travel_time_min
            resolution = (
                primary.resolution_time_min if primary else settings.triage.default_resolution_time_min
            )
            outcome = escalate(
                severity=request.severity,
                hours_since_request=hours_between(request.created_at, reference),
                hours_since_promotion=hours_between(
                    request.promoted_at or request.created_at, reference
                ),
                travel_time_min=travel,
                resolution_time_min=resolution,
                scale_factor=settings.queue.scale_factor,
            )
            updated = self._requests.apply_escalation(
                request.request_id,
                severity=outcome.severity,
                heap_key=outcome.heap_key,
                stage=outcome.stage,
                promoted=outcome.promoted,
            )
            if updated is None:
                continue
            changed += 1
            if outcome.promoted:
                promotions += 1
                log.warning(
                    "%s promoted to %s after %.1f h waiting",
                    request.request_id,
                    outcome.severity.value,
                    hours_between(request.created_at, reference),
                )
                audit.record(
                    from_agent="escalation_scheduler",
                    to_agent="priority_queue",
                    reason=f"promoted to {outcome.severity.value}",
                    request_id=request.request_id,
                    stage=outcome.stage,
                )
                if self._bus is not None:
                    self._bus.publish(
                        EVENT_ESCALATED,
                        request_id=request.request_id,
                        severity=outcome.severity.value,
                        stage=outcome.stage,
                    )

        if changed and self._bus is not None:
            self._bus.publish(EVENT_QUEUE_CHANGED, escalated=changed, promoted=promotions)
        if changed:
            log.info("Escalation pass updated %d request(s), %d promoted", changed, promotions)
        return changed
