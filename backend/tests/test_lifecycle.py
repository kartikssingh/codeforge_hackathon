"""End-to-end request lifecycle through the hub, with no models installed."""

from __future__ import annotations

import pytest

from aria.core.errors import ConflictError, NotFoundError
from aria.domain.enums import RequestStatus, Severity, VolunteerStatus
from aria.schemas import ApproveRequest, ItemMovement, MaterialSelection, OverrideRequest

CARDIAC = "He collapsed in the hall, he is not breathing and has no pulse"
FRACTURE = "Elderly woman fell, her leg looks wrong and she cannot walk"
SUPPLIES = "Family of four needs water and blankets, nothing to eat since yesterday"


def _stock(hub, item):
    row = next(r for r in hub.inventory.all() if r["item"] == item)
    return row["available"], row["reserved"]


def test_text_intake_produces_a_reviewable_request(hub):
    result = hub.intake_text(CARDIAC)
    request = result.request

    assert request.status is RequestStatus.AWAITING_REVIEW
    assert request.severity is Severity.CRITICAL
    assert request.situations
    # No LLM in the test environment: the rule engine carried the triage.
    assert request.degraded is True
    assert all(s.origin == "rules" for s in request.situations)
    assert request.handoff_logs  # explainability trail is populated


def test_nothing_is_reserved_before_a_human_approves(hub):
    before = _stock(hub, "CPR Mask")
    hub.intake_text(CARDIAC)
    assert _stock(hub, "CPR Mask") == before


def test_approval_reserves_stock_and_dispatches(hub):
    request = hub.intake_text(CARDIAC).request
    available_before, reserved_before = _stock(hub, "CPR Mask")

    outcome = hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))
    approved = outcome["request"]

    assert approved.status is RequestStatus.ASSIGNED
    assert approved.assigned_volunteer is not None
    assert approved.expected_return is not None
    available_after, reserved_after = _stock(hub, "CPR Mask")
    assert available_after == available_before - 1
    assert reserved_after == reserved_before + 1


def test_approving_twice_is_refused(hub):
    request = hub.intake_text(CARDIAC).request
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))

    with pytest.raises(ConflictError):
        hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))


def test_material_overrides_change_what_is_reserved(hub):
    request = hub.intake_text(CARDIAC).request

    hub.approve(
        request.request_id,
        ApproveRequest(
            selected_indices=[0],
            material_overrides=[
                MaterialSelection(item="CPR Mask", quantity=0),
                MaterialSelection(item="Thermal Blanket", quantity=2),
            ],
        ),
    )

    taken = {m.item: m.quantity for m in hub.requests.get(request.request_id).items_taken}
    assert "CPR Mask" not in taken
    assert taken.get("Thermal Blanket") == 2


def test_priority_order_is_by_severity_then_arrival(hub):
    low = hub.intake_text(SUPPLIES).request
    high = hub.intake_text(FRACTURE).request
    critical = hub.intake_text(CARDIAC).request

    hub.set_volunteer_count(0)  # nothing can be dispatched yet
    for request in (low, high, critical):
        hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))

    assert hub.requests.queued_ids() == [
        critical.request_id,
        high.request_id,
        low.request_id,
    ]
    assert hub.requests.next_dispatchable().request_id == critical.request_id


def test_volunteers_take_the_most_urgent_work_first(hub):
    hub.set_volunteer_count(0)
    low = hub.intake_text(SUPPLIES).request
    critical = hub.intake_text(CARDIAC).request
    hub.approve(low.request_id, ApproveRequest(selected_indices=[0]))
    hub.approve(critical.request_id, ApproveRequest(selected_indices=[0]))

    hub.set_volunteer_count(1)

    assert hub.requests.get(critical.request_id).status is RequestStatus.ASSIGNED
    assert hub.requests.get(low.request_id).status is RequestStatus.QUEUED


def test_return_closes_the_request_and_frees_the_volunteer(hub):
    request = hub.intake_text(FRACTURE).request
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))
    volunteer = next(v for v in hub.dispatch.all() if v.status is VolunteerStatus.BUSY)
    taken = list(volunteer.items_taken)

    hub.volunteer_return(volunteer.volunteer_id, taken)  # everything came back

    closed = hub.requests.get(request.request_id)
    assert closed.status is RequestStatus.RESOLVED
    assert closed.actual_return is not None
    assert [m.item for m in closed.items_returned] == [m.item for m in taken]
    assert hub.dispatch.get(volunteer.volunteer_id).status is VolunteerStatus.AVAILABLE
    assert hub.dispatch.get(volunteer.volunteer_id).missions_completed == 1


def test_returning_frees_the_volunteer_for_the_next_task(hub):
    hub.set_volunteer_count(1)
    first = hub.intake_text(CARDIAC).request
    second = hub.intake_text(FRACTURE).request
    hub.approve(first.request_id, ApproveRequest(selected_indices=[0]))
    hub.approve(second.request_id, ApproveRequest(selected_indices=[0]))
    assert hub.requests.get(second.request_id).status is RequestStatus.QUEUED

    volunteer = hub.dispatch.all()[0]
    hub.volunteer_return(volunteer.volunteer_id, volunteer.items_taken)

    assert hub.requests.get(second.request_id).status is RequestStatus.ASSIGNED


def test_cancelling_releases_the_reservation(hub):
    hub.set_volunteer_count(0)
    request = hub.intake_text(FRACTURE).request
    before = _stock(hub, "Leg Splint")
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))
    assert _stock(hub, "Leg Splint") != before

    hub.cancel(request.request_id, "Family reached the clinic themselves")

    assert hub.requests.get(request.request_id).status is RequestStatus.CANCELLED
    assert _stock(hub, "Leg Splint") == before


def test_cannot_cancel_a_request_a_volunteer_is_already_attending(hub):
    request = hub.intake_text(CARDIAC).request
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))

    with pytest.raises(ConflictError):
        hub.cancel(request.request_id, "changed my mind")


def test_manual_override_supersedes_the_ai_assessment(hub):
    source = hub.intake_text(SUPPLIES).request

    outcome = hub.override(
        source.request_id,
        OverrideRequest(
            condition="Bridge collapse on Route 9",
            severity=Severity.CRITICAL,
            resources=[MaterialSelection(item="Flashlight", quantity=2)],
            instructions=["Set up a cordon", "Count the people cut off"],
        ),
    )
    override = outcome["request"]

    assert override.request_id != source.request_id
    assert override.severity is Severity.CRITICAL
    assert override.situations[0].origin == "manual"
    assert hub.requests.get(source.request_id).status is RequestStatus.SUPERSEDED
    assert {m.item: m.quantity for m in override.items_taken} == {"Flashlight": 2}


def test_busy_volunteers_are_never_removed(hub):
    request = hub.intake_text(CARDIAC).request
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))
    busy = next(v for v in hub.dispatch.all() if v.status is VolunteerStatus.BUSY)

    hub.set_volunteer_count(0)
    assert busy.volunteer_id in {v.volunteer_id for v in hub.dispatch.all()}

    with pytest.raises(ConflictError):
        hub.remove_volunteer(busy.volunteer_id)


def test_unknown_ids_raise_not_found(hub):
    with pytest.raises(NotFoundError):
        hub.requests.get("REQ-NOPE")
    with pytest.raises(NotFoundError):
        hub.dispatch.get("V-99")


def test_escalation_promotes_a_forgotten_request(hub, monkeypatch):
    from datetime import timedelta

    from aria.utils.timeutil import now

    hub.set_volunteer_count(0)
    request = hub.intake_text(SUPPLIES).request
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))
    assert hub.requests.get(request.request_id).severity is Severity.LOW
    key_before = hub.requests.get(request.request_id).heap_key

    # Pretend it has been waiting seven hours.
    stored = hub.requests.get(request.request_id)
    stored.created_at = now() - timedelta(hours=7)

    changed = hub.escalation.run_once()

    escalated = hub.requests.get(request.request_id)
    assert changed == 1
    assert escalated.severity is Severity.MEDIUM
    assert escalated.heap_key > key_before
    assert hub.requests.queued_ids() == [request.request_id]


def test_board_snapshot_is_self_consistent(hub):
    hub.intake_text(CARDIAC)
    board = hub.board()

    assert len(board.queue) == 1
    assert board.volunteers
    assert board.inventory
    assert board.metrics["requests"]["awaiting_review"] == 1


def test_state_snapshot_round_trips(hub, tmp_path):
    from aria.services.persistence import PersistenceService
    from aria.services.requests import RequestService

    request = hub.intake_text(CARDIAC).request
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))

    store = PersistenceService(tmp_path / "state.json", enabled=True)
    store.register("requests", hub.requests)
    store.register("volunteers", hub.dispatch)
    store.register("inventory", hub.inventory)
    assert store.flush(force=True) is True

    fresh_requests = RequestService(hub.inventory, bus=None)
    reader = PersistenceService(tmp_path / "state.json", enabled=True)
    reader.register("requests", fresh_requests)
    restored = reader.load()

    assert restored["requests"] == 1
    recovered = fresh_requests.get(request.request_id)
    assert recovered.status is RequestStatus.ASSIGNED
    assert recovered.summary == request.summary


def test_returned_items_beyond_what_was_taken_go_to_the_buffer(hub):
    request = hub.intake_text(CARDIAC).request
    hub.approve(request.request_id, ApproveRequest(selected_indices=[0]))
    volunteer = next(v for v in hub.dispatch.all() if v.status is VolunteerStatus.BUSY)

    hub.volunteer_return(
        volunteer.volunteer_id,
        list(volunteer.items_taken) + [ItemMovement(item="Energy Bar", quantity=3)],
    )

    rows = {r["item"]: r for r in hub.inventory.all()}
    assert rows["Energy Bar"]["available"] + rows["Energy Bar"]["reserved"] <= rows["Energy Bar"]["total"]
