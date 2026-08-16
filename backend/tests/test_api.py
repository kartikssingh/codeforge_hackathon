"""HTTP surface — routing, status codes and the error envelope."""

from __future__ import annotations

CARDIAC = "He collapsed, he is not breathing and has no pulse"


def _intake(client, text: str = CARDIAC) -> dict:
    response = client.post("/pipeline/text", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def test_health_is_cheap_and_always_ok(api_client):
    payload = api_client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["version"]


def test_health_detail_reports_each_component(api_client):
    payload = api_client.get("/health/detail").json()
    names = {component["name"] for component in payload["components"]}

    assert {"inventory", "triage_rules", "language_model", "speech_to_text"} <= names
    # No models installed in the test environment, but the core still works.
    assert payload["status"] in {"ok", "degraded"}


def test_text_intake_returns_a_full_request(api_client):
    payload = _intake(api_client)

    assert payload["request"]["status"] == "AWAITING_REVIEW"
    assert payload["request"]["severity"] == "CRITICAL"
    assert payload["request"]["situations"]
    assert payload["timings_ms"]["total"] >= 0


def test_text_intake_rejects_an_empty_report(api_client):
    response = api_client.post("/pipeline/text", json={"text": "  "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_approve_returns_the_whole_board(api_client):
    request_id = _intake(api_client)["request"]["request_id"]

    response = api_client.post(
        f"/requests/{request_id}/approve", json={"selected_indices": [0]}
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["request"]["status"] == "ASSIGNED"
    assert payload["board"]["queue"]
    assert payload["board"]["volunteers"]
    assert payload["board"]["inventory"]
    assert payload["detail"]["reservation"]["fully_satisfied"] is True


def test_missing_request_returns_the_error_envelope(api_client):
    response = api_client.get("/requests/REQ-NOPE")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_approving_twice_conflicts(api_client):
    request_id = _intake(api_client)["request"]["request_id"]
    api_client.post(f"/requests/{request_id}/approve", json={"selected_indices": [0]})

    response = api_client.post(
        f"/requests/{request_id}/approve", json={"selected_indices": [0]}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_volunteer_return_flow(api_client):
    request_id = _intake(api_client)["request"]["request_id"]
    board = api_client.post(
        f"/requests/{request_id}/approve", json={"selected_indices": [0]}
    ).json()["board"]
    volunteer = next(v for v in board["volunteers"] if v["status"] == "BUSY")

    response = api_client.post(
        f"/volunteers/{volunteer['volunteer_id']}/return",
        json={"returned_items": volunteer["items_taken"]},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["request"]["status"] == "RESOLVED"
    assert payload["detail"]["settlement"]["restored"]


def test_volunteer_return_requires_an_active_mission(api_client):
    response = api_client.post("/volunteers/V-01/return", json={"returned_items": []})
    assert response.status_code == 409


def test_roster_can_be_resized_and_named(api_client):
    api_client.post("/volunteers/count", json={"count": 2})
    api_client.post("/volunteers", json={"name": "Priya"})

    volunteers = api_client.get("/volunteers").json()
    assert len(volunteers) == 3
    assert "Priya" in {v["name"] for v in volunteers}


def test_inventory_endpoints(api_client):
    listing = api_client.get("/inventory").json()
    assert listing["stats"]["items"] == 12

    created = api_client.post(
        "/inventory", json={"item": "Burn Dressing", "capacity": 6}
    )
    assert created.status_code == 200

    added = api_client.post("/inventory/Burn Dressing/stock", json={"quantity": 1})
    assert added.status_code == 422  # already at capacity

    removed = api_client.delete("/inventory/Burn Dressing")
    assert removed.status_code == 200


def test_refill_restores_capacity(api_client):
    api_client.post("/inventory/refill", json={"mode": "daily"})

    rows = api_client.get("/inventory").json()["inventory"]
    assert all(row["available"] == row["total"] for row in rows)
    assert all(row["reserved"] == 0 for row in rows)


def test_metrics_and_logs(api_client):
    _intake(api_client)

    metrics = api_client.get("/metrics").json()
    assert metrics["requests"]["awaiting_review"] == 1
    assert "sla" in metrics

    logs = api_client.get("/logs").json()["logs"]
    assert any(entry["to_agent"] for entry in logs)


def test_frontend_settings_expose_server_thresholds(api_client):
    payload = api_client.get("/settings/frontend").json()

    assert payload["thresholds"]["low_stock_pct"] == 20
    assert payload["sla_minutes"]["CRITICAL"] >= 1
    assert ".wav" in payload["audio"]["accepted_extensions"]


def test_board_endpoint_matches_the_queue_endpoint(api_client):
    _intake(api_client)

    board = api_client.get("/board").json()
    queue = api_client.get("/queue").json()["queue"]

    assert [r["request_id"] for r in board["queue"]] == [r["request_id"] for r in queue]
