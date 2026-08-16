"""Operational metrics for the dashboard and after-action review.

Everything here is derived on demand from live state — there is no separate
counter to drift out of sync.  The numbers that matter to a shelter manager are
not "requests per second" but: who is waiting, how long have they waited, and is
anything about to breach its response target.
"""

from __future__ import annotations

from typing import Any, Optional

from aria.config import settings
from aria.domain.enums import RequestStatus, Severity
from aria.schemas import EmergencyRequest
from aria.services.dispatch import DispatchService
from aria.services.inventory import InventoryService
from aria.services.requests import RequestService
from aria.utils.timeutil import minutes_between, now


class MetricsService:
    def __init__(
        self,
        requests: RequestService,
        dispatch: DispatchService,
        inventory: InventoryService,
    ) -> None:
        self._requests = requests
        self._dispatch = dispatch
        self._inventory = inventory

    def summary(self) -> dict[str, Any]:
        reference = now()
        everything = self._requests.list()
        open_requests = [r for r in everything if r.status.is_open]
        resolved = [r for r in everything if r.status == RequestStatus.RESOLVED]

        by_status: dict[str, int] = {status.value: 0 for status in RequestStatus}
        for request in everything:
            by_status[request.status.value] += 1

        by_severity: dict[str, int] = {severity.value: 0 for severity in Severity}
        for request in open_requests:
            by_severity[request.severity.value] += 1

        waits = [request.waited_minutes(reference) for request in open_requests]
        breaches = [r for r in open_requests if self._is_breaching(r, reference)]

        return {
            "generated_at": reference.isoformat(),
            "requests": {
                "total": len(everything),
                "open": len(open_requests),
                "awaiting_review": by_status[RequestStatus.AWAITING_REVIEW.value],
                "queued": by_status[RequestStatus.QUEUED.value],
                "assigned": by_status[RequestStatus.ASSIGNED.value],
                "resolved": len(resolved),
                "cancelled": by_status[RequestStatus.CANCELLED.value],
                "by_status": by_status,
                "open_by_severity": by_severity,
                "escalated": sum(1 for r in open_requests if r.escalation_stage > 0),
            },
            "timing_minutes": {
                "longest_open_wait": round(max(waits), 1) if waits else 0.0,
                "median_open_wait": round(_median(waits), 1),
                "avg_time_to_approve": _avg(
                    [
                        minutes_between(r.created_at, r.approved_at)
                        for r in everything
                        if r.approved_at
                    ]
                ),
                "avg_time_to_dispatch": _avg(
                    [
                        minutes_between(r.approved_at, r.assigned_at)
                        for r in everything
                        if r.approved_at and r.assigned_at
                    ]
                ),
                "avg_time_to_resolve": _avg(
                    [
                        minutes_between(r.created_at, r.resolved_at)
                        for r in resolved
                        if r.resolved_at
                    ]
                ),
            },
            "sla": {
                "targets_minutes": {
                    severity.value: settings.sla_minutes(severity.value) for severity in Severity
                },
                "breaching_now": len(breaches),
                "breaching_ids": [r.request_id for r in breaches],
            },
            "volunteers": self._dispatch.stats(),
            "inventory": self._inventory.stats(),
        }

    def _is_breaching(self, request: EmergencyRequest, reference: Optional[Any] = None) -> bool:
        """Open longer than its severity's response target, and not yet en route."""
        if request.status == RequestStatus.ASSIGNED:
            return False
        target = settings.sla_minutes(request.severity.value)
        return request.waited_minutes(reference) > target


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
