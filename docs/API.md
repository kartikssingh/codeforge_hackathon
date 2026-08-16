# API reference

Base URL `http://127.0.0.1:8000` · interactive docs at `/docs` · no
authentication by design (the API is reachable only from the machine it runs on
— do not bind it to `0.0.0.0`).

**Conventions**

- Times are ISO-8601 with an offset: `2026-08-16T17:48:27.965157+05:30`.
- Severities: `CRITICAL` `HIGH` `MEDIUM` `LOW`.
- Statuses: `AWAITING_REVIEW` `QUEUED` `ASSIGNED` `RESOLVED` `CANCELLED` `SUPERSEDED`.
- Every mutating endpoint returns the whole board so the UI stays consistent.
- Errors always look like:
  ```json
  {"error": {"code": "conflict", "message": "…", "detail": {}}}
  ```

| Code | Status | Meaning |
|---|---|---|
| `not_found` | 404 | No such request, volunteer or item |
| `conflict` | 409 | Right object, wrong state |
| `insufficient_stock` | 409 | Not enough stock for the reservation |
| `invalid_request` | 422 | Body failed validation |
| `agent_unavailable` | 503 | A required model or runtime is missing |
| `internal_error` | 500 | Bug — see `backend/logs/aria.log` |

---

## Intake

### `POST /pipeline`

Audio report. Runs denoise → transcribe → retrieve → triage → logistics.

```json
{"audio_b64": "UklGRi4A…", "filename": "call_017.wav", "npu_mode": false}
```

Returns `IntakeResponse`:

```json
{
  "request": { "…EmergencyRequest…" },
  "timings_ms": {"decode": 4, "denoise": 812, "transcribe": 6120,
                 "retrieval": 240, "triage": 4310, "logistics": 1, "total": 11487},
  "degraded": false,
  "notes": []
}
```

`503 agent_unavailable` when no speech-to-text model is installed; the detail
carries `hint: "Type the report into POST /pipeline/text instead."`

### `POST /pipeline/text`

Typed report — radio traffic, a runner's message, a paper form. Needs no audio
stack, and is the path the test suite uses.

```json
{"text": "Bay 12 — elderly man collapsed, not breathing", "npu_mode": false}
```

Same response shape.

---

## The board

### `GET /board?metrics=true`

One consistent snapshot of everything the dashboard renders.

```json
{
  "queue": [ "…EmergencyRequest…" ],
  "volunteers": [ "…Volunteer…" ],
  "inventory": [ "…InventoryRow…" ],
  "buffer": [{"item": "AED", "quantity": 1, "capacity": 100}],
  "metrics": { "…see /metrics…" }
}
```

Ordering: requests needing a decision first, then by heap key, then by arrival.

### `GET /events`

Server-Sent Events. Each frame names what changed; fetch `/board` in response.

```
event: ready
data: {"type":"ready","at":"…","payload":{"subscribers":1}}

event: queue.changed
data: {"type":"queue.changed","at":"…","payload":{"request_id":"REQ-A1B2C3"}}

event: heartbeat
data: {"type":"heartbeat","at":"…","payload":{"ok":true}}
```

Event types: `queue.changed` · `volunteers.changed` · `inventory.changed` ·
`request.created` · `request.updated` · `request.escalated` · `alert` ·
`heartbeat`.

---

## Requests

### `GET /queue`
`{"queue": [...]}` — open requests in priority order.

### `GET /requests?status=QUEUED&limit=200`
All requests, optionally filtered by status.

### `GET /requests/history?limit=50`
Closed requests, most recently resolved first.

### `GET /requests/{id}`
One request. `404` if unknown.

### `POST /requests/{id}/approve`

The human-in-the-loop gate. Confirms situations, reserves their materials
atomically, queues the request, and dispatches if a volunteer is free.

```json
{
  "selected_indices": [0, 2],
  "material_overrides": [
    {"item": "CPR Mask", "quantity": 0},
    {"item": "Thermal Blanket", "quantity": 2}
  ],
  "note": "second situation confirmed by the caller"
}
```

`material_overrides` replaces the quantity for a line (0 means "do not take
it") and may add an item no situation asked for.

```json
{
  "request": { "…status now QUEUED or ASSIGNED…" },
  "board": { "…" },
  "detail": {
    "reservation": {
      "lines": [
        {"item": "AED", "matched_item": "AED", "requested": 1,
         "reserved": 0, "shortfall": 1, "ok": false, "reason": "only 0 of 1 in stock"}
      ],
      "fully_satisfied": false
    },
    "assignments": [{"volunteer_id": "V-01", "request_id": "REQ-A1B2C3"}]
  }
}
```

A shortfall does not block dispatch — a volunteer with three of five bandages is
still worth sending — but it is reported explicitly, never hidden.

`409 conflict` if the request is not `AWAITING_REVIEW`.

### `POST /requests/{id}/override`

Replaces the AI assessment with the manager's own. Creates a new request, marks
the original `SUPERSEDED`, releases its holds and queues the override.

```json
{
  "condition": "Bridge collapse on Route 9",
  "severity": "CRITICAL",
  "travel_time_min": 25,
  "resolution_time_min": 60,
  "instructions": ["Set up a cordon at the north end", "Count the people cut off"],
  "resources": [{"item": "Flashlight", "quantity": 2}],
  "notes": ""
}
```

### `POST /requests/{id}/cancel`

```json
{"reason": "Family reached the clinic themselves"}
```

Held stock returns to available. `409` while a volunteer is already attending —
record their return first.

---

## Volunteers

### `GET /volunteers`

```json
[{
  "volunteer_id": "V-01", "name": "Priya", "status": "BUSY",
  "request_id": "REQ-A1B2C3", "request_summary": "Cardiac arrest",
  "assigned_at": "…", "expected_return": "…",
  "items_taken": [{"item": "AED", "quantity": 1}],
  "missions_completed": 3, "on_roster_since": "…"
}]
```

### `POST /volunteers` — `{"name": "Priya"}`
### `POST /volunteers/count` — `{"count": 5}`
Resize the roster. Volunteers out on a mission are never removed.

### `PATCH /volunteers/{id}` — `{"status": "OFF_DUTY"}`
Rest someone without losing their history. `BUSY` cannot be set by hand.

### `DELETE /volunteers/{id}`
`409` while they are out.

### `POST /volunteers/{id}/return`

*Back at base.* Restores what came back, writes off the difference as consumed,
closes the request, frees the volunteer and re-dispatches.

```json
{"returned_items": [{"item": "AED", "quantity": 1}], "note": "handed over to medics"}
```

```json
{
  "request": { "…status RESOLVED…" },
  "board": { "…" },
  "detail": {
    "volunteer_id": "V-01",
    "settlement": {
      "restored": [{"item": "AED", "quantity": 1}],
      "consumed": [{"item": "Sterile Gauze", "quantity": 4}],
      "buffered": []
    },
    "new_assignments": [{"volunteer_id": "V-01", "request_id": "REQ-D4E5F6"}]
  }
}
```

`buffered` is stock that came back but no longer fits its bin.

---

## Inventory

### `GET /inventory`

```json
{
  "inventory": [{
    "item": "AED", "available": 1, "reserved": 1, "total": 2,
    "bin": "A-01", "category": "Medical",
    "committed": 2, "capacity_pct": 50, "status": "OK"
  }],
  "buffer": [],
  "stats": {"items": 22, "units_total": 340, "units_available": 291,
            "units_reserved": 22, "fill_pct": 86, "low_stock_items": 3}
}
```

`status` ∈ `OK` `LOW` `OUT_OF_STOCK` `ALL_RESERVED` `UNTRACKED`.

### `GET /inventory/low` · `GET /inventory/buffer` · `GET /inventory/history?limit=100`

History records every movement: `reserve` `release` `consume` `restore`
`buffer` `add_stock` `create` `delete` `daily_refill` `partial_refill`.

### `POST /inventory`
```json
{"item": "Burn Dressing", "capacity": 12, "bin": "A-05", "category": "Medical"}
```

### `POST /inventory/{item}/stock` — `{"quantity": 5}`
Refused past capacity: reserved units still occupy the bin.

### `POST /inventory/refill` — `{"mode": "daily"}` or `{"mode": "partial"}`
`daily` returns everything to capacity and clears holds; `partial` tops up only
what is at or below the refill threshold.

### `DELETE /inventory/{item}`
`422` while units are reserved.

---

## Observability

### `GET /metrics`

```json
{
  "generated_at": "…",
  "requests": {"total": 12, "open": 4, "awaiting_review": 1, "queued": 1,
               "assigned": 2, "resolved": 7, "cancelled": 1,
               "open_by_severity": {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 1, "LOW": 0},
               "escalated": 1},
  "timing_minutes": {"longest_open_wait": 42.5, "median_open_wait": 12.0,
                     "avg_time_to_approve": 1.4, "avg_time_to_dispatch": 0.2,
                     "avg_time_to_resolve": 38.7},
  "sla": {"targets_minutes": {"CRITICAL": 5, "HIGH": 30, "MEDIUM": 120, "LOW": 360},
          "breaching_now": 1, "breaching_ids": ["REQ-A1B2C3"]},
  "volunteers": {"total": 3, "busy": 2, "available": 1, "off_duty": 0,
                 "overdue": 1, "missions_completed": 7, "utilisation_pct": 67},
  "inventory": {"…as above…"}
}
```

An SLA breach counts a request that has waited past its severity's target
*without a volunteer en route*.

### `GET /logs?limit=100&request_id=REQ-A1B2C3`

The agent hand-off trail — the answer to "why did it decide that?"

```json
{"logs": [{
  "at": "…", "from_agent": "RETRIEVAL_AGENT", "to_agent": "VAGUENESS_AGENT",
  "reason": "top score 0.31 below threshold", "request_id": "REQ-A1B2C3",
  "duration_ms": 3554,
  "detail": {"hypotheses": ["cardiac arrest", "severe bleeding"]}
}]}
```

Also appended to `backend/logs/handoffs.jsonl`.

### `GET /health` · `GET /health/detail`

`/health` is trivial and touches no locks — the Electron boot poll hits it every
500 ms. `/health/detail` reports each component: `ok`, `degraded` (core works,
some capability missing) or `down`.

---

## Admin

### `POST /admin/reload?rebuild_index=false`
Re-reads the inventory CSV and the triage rules, clears the LLM cache, and
rebuilds the protocol index if asked. Use after copying new PDFs onto the
machine.

### `POST /admin/snapshot`
Write the state snapshot now instead of waiting for the flusher.

### `POST /admin/reset`
Cancel every open request and delete the snapshot. Requests with a volunteer
already in the field are left alone.

---

## Object reference

### EmergencyRequest

```json
{
  "request_id": "REQ-A1B2C3",
  "created_at": "2026-08-16T17:48:27.965157+05:30",
  "transcript": "He collapsed and is not breathing",
  "intake_mode": "audio",
  "is_vague": false,
  "retrieval_top_score": 0.71,
  "summary": "Cardiac arrest / unresponsive casualty",
  "situations": [ "…Situation…" ],
  "status": "ASSIGNED",
  "severity": "CRITICAL",
  "heap_key": 99965.0,
  "escalation_stage": 0,
  "promoted_at": null,
  "approved_at": "…", "assigned_volunteer": "V-01", "assigned_at": "…",
  "expected_return": "…", "actual_return": null, "resolved_at": null,
  "closed_reason": null,
  "items_taken": [{"item": "AED", "quantity": 1}],
  "items_returned": [], "items_consumed": [],
  "handoff_logs": [ "…HandoffLog…" ],
  "degraded": false,
  "notes": []
}
```

### Situation

```json
{
  "label": "Cardiac arrest / unresponsive casualty",
  "severity": "CRITICAL",
  "severity_score": 100,
  "confidence": 0.93,
  "travel_time_min": 5,
  "resolution_time_min": 25,
  "heap_key": 99965.0,
  "materials": [{"item": "AED", "quantity": 1, "available": true,
                 "available_qty": 2, "bin": "A-01", "matched_item": "AED"}],
  "instructions": ["Confirm the scene is safe…", "Start chest compressions…"],
  "reasoning": "Protocol rule 'cardiac-arrest' matched: “no pulse”, “not breathing”",
  "source_chunks": [{"source": "QR-02_CPR_AED.pdf", "page": "3", "score": 0.81}],
  "origin": "llm+rules",
  "selected": true
}
```

`origin` ∈ `llm` `rules` `llm+rules` `manual` `fallback` — which engine produced
the hypothesis, surfaced in the UI so the manager knows what they are reading.
