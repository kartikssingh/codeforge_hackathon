# Architecture

How ARIA is put together, and why each piece is the way it is.

---

## 1. Two processes

```
┌──────────────────────────────────────────────────────────────────────┐
│ ELECTRON MAIN                              electron/main.js          │
│   spawns backend/main.py · polls /health · owns the window           │
│   kills the backend on quit (SIGTERM, then SIGKILL after 4 s)        │
└───────────────┬──────────────────────────────────────────────────────┘
                │ contextBridge (no Node in the renderer, no CORS surface)
┌───────────────▼──────────────────────────────────────────────────────┐
│ PRELOAD                                    electron/preload.js       │
│   the ONLY code that touches the network                             │
│   window.aria.*  → HTTP via Node's http module                       │
│   window.aria.onEvent(cb) → SSE with automatic reconnection          │
└───────────────┬──────────────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────────────┐
│ RENDERER                                   frontend/js/*.js          │
│   classic scripts on window.ARIA (ES modules do not load over file://)│
│   util → toast → api → store → views → modals → actions → app        │
│   CSP: default-src 'none'; script-src 'self'                          │
└───────────────┬──────────────────────────────────────────────────────┘
                │ HTTP + SSE, 127.0.0.1 only
┌───────────────▼──────────────────────────────────────────────────────┐
│ FASTAPI BACKEND                            backend/aria/             │
│   api → services → agents → domain                                    │
└──────────────────────────────────────────────────────────────────────┘
```

The renderer cannot reach the network at all. Its CSP has no `connect-src`, so
even if a transcript contained a script tag *and* escaped `esc()`, it would have
nowhere to send anything.

---

## 2. Backend layers

Dependencies point one way only: `api → services → agents → domain`. Nothing in
`domain` or `utils` imports FastAPI, and nothing in `agents` knows about HTTP.

| Layer | Package | Responsibility | May import |
|---|---|---|---|
| Domain | `aria/domain` | Severity, heap key, escalation policy | nothing (stdlib only) |
| Core | `aria/core` | Errors, logging, indexed heap, event bus | domain, utils |
| Utils | `aria/utils` | Time, text matching, audio temp files | stdlib (+ optional rapidfuzz) |
| Contracts | `aria/schemas.py` | Pydantic models for every payload | domain, utils |
| LLM | `aria/llm` | Ollama / OpenVINO / ONNX behind one interface | core, config |
| Agents | `aria/agents` | One pipeline step each | core, llm, schemas, services.inventory |
| Services | `aria/services` | State, lifecycle, orchestration | agents, core, schemas |
| API | `aria/api` | Thin HTTP adapters | services, schemas |

`services/hub.py` is the composition root. It owns every service instance, wires
them together, and hosts the flows that cross service boundaries — approve then
dispatch, record a return then re-dispatch. That is what keeps `RequestService`
unaware of volunteers and `DispatchService` unaware of HTTP.

---

## 3. The pipeline

`agents/pipeline.py` is the orchestrator. Each step is timed and each hand-off
recorded.

```
decode ─▶ denoise ─▶ transcribe ─▶ retrieve ─▶ [expand] ─▶ triage ─▶ logistics
```

| Step | Module | If it is unavailable |
|---|---|---|
| denoise | `agents/denoise.py` | copies the raw audio through, flags degraded |
| transcribe | `agents/transcribe.py` | **503** naming `POST /pipeline/text` |
| retrieve | `agents/retrieval.py` | empty result marked vague; rules carry triage |
| expand | `agents/vagueness.py` | hypotheses come from the rule engine instead |
| triage | `agents/triage.py` | rule engine alone; then a protocol-derived fallback |
| logistics | `agents/logistics.py` | always available (reads the ledger) |

Text intake enters at `retrieve`, skipping the two audio steps entirely.

### Triage merge

The LLM differential and the rule differential are unioned and deduplicated by
label (normalised, then fuzzy above 72 % similarity, so "Cardiac arrest" and
"Cardiac arrest / unresponsive casualty" collapse into one option). When both
engines propose the same condition, the more confident wins but inherits the
other's materials and citations — the rule engine knows the shelf, the model
read the specific report. On a severity disagreement the **more severe**
assessment wins.

### Vagueness expansion

If the top retrieval relevance is below `ARIA_CONFIDENCE_THRESHOLD` (0.55), the
report is expanded: hypotheses are generated per severity level and retrieval
runs again for each, severity-first so CRITICAL candidates always get a query
even on a small budget. Chunks are merged, deduplicated by content and capped.

---

## 4. State

All state is in memory, owned by services, snapshotted to disk.

```
Hub
├── InventoryService   rows[] + buffer{} + history[]   → data/inventory.csv (atomic)
├── RequestService     requests{} + IndexedPriorityQueue
├── DispatchService    volunteers{}
├── EscalationService  daemon thread, 60 s
├── MetricsService     derived on demand, stores nothing
└── PersistenceService debounced JSON snapshots        → state/aria_state.json (atomic)
```

### Request lifecycle

```
AWAITING_REVIEW ──approve──▶ QUEUED ──dispatch──▶ ASSIGNED ──return──▶ RESOLVED
       │                        │                     │
       ├──override──▶ SUPERSEDED│                     │
       └──cancel────▶ CANCELLED ◀┴───cancel (refused while assigned)
```

Only `QUEUED` requests sit in the heap. `AWAITING_REVIEW` requests still
escalate — an unreviewed report gets more urgent while it waits — but cannot be
dispatched until a human confirms them.

### The heap

`core/priority_queue.py` is an indexed max-heap with lazy deletion: each key
owns one entry, updating invalidates the old entry and pushes a new one, and
invalidated entries are compacted once they are both numerous and a majority.
Entries are `[-priority, order, seq, key]` — `order` is the arrival timestamp
(FIFO tie-break) and `seq` makes the comparison total so the heap never falls
through to comparing strings.

The previous implementation rebuilt the entire heap on every escalation tick
and linear-scanned a sorted copy on every dispatch.

### Persistence

Services mark state dirty; a daemon thread flushes at most every two seconds,
writing to a temp file and `os.replace`-ing it into place. A burst of twenty
inventory movements costs one write, and a power cut can never leave a truncated
snapshot. Version-stamped: a snapshot from an older schema is ignored rather
than half-loaded.

---

## 5. Concurrency

| Concern | Mechanism |
|---|---|
| Shared mutable state | `threading.RLock` per service; no service reaches into another's state |
| Heavy pipeline work | `run_in_threadpool` — never blocks the event loop |
| Concurrent pipelines | `asyncio.Semaphore`, default 1 (two CPU inference jobs finish slower together) |
| Escalation | daemon thread with `Event.wait()`; deterministic shutdown |
| Snapshot writes | dedicated thread, debounced, atomic |
| Worker → event loop | bounded `queue.Queue` per SSE subscriber; oldest dropped if a client stalls |

The old build ran Whisper and the LLM inside `async def` handlers, so the event
loop — and therefore `/health`, the queue polling and the whole UI — froze for
the 10–25 s of every pipeline run.

---

## 6. Live updates

```
service mutates state
   └─▶ EventBus.publish("queue.changed", …)
          └─▶ per-subscriber queue.Queue (bounded, drops oldest)
                 └─▶ GET /events drains on the event loop → SSE frame
                        └─▶ preload parses frames → renderer handler
                               └─▶ ARIA.app.refreshBoard() (coalesced)
```

Events say *that* something changed, not *what* the new state is. The renderer
responds by fetching `/board` once — a single consistent snapshot — instead of
trying to patch state from event payloads. Polling remains as a slow safety net
(10 s when the stream is down, 30 s when it is healthy).

Every mutating endpoint also returns the whole board in its response, so the UI
never assembles a view from several requests taken at different moments.

---

## 7. Error model

`core/errors.py` defines the exception hierarchy; one handler in `api/__init__`
turns it into JSON. Services raise; routes stay free of HTTP concerns.

| Exception | Status | Meaning |
|---|---|---|
| `NotFoundError` | 404 | No such request, volunteer or item |
| `ConflictError` | 409 | Right object, wrong state (approving twice, cancelling a dispatched request) |
| `InsufficientStockError` | 409 | Not enough stock for an atomic reservation |
| `ValidationError` | 422 | Bad input the schema could not catch |
| `AgentUnavailableError` | 503 | A model or runtime is missing — a degraded-mode signal, not a bug |
| `PipelineError` | 500 | Genuine failure |

Everything reaching the generic handler is logged with a traceback and answered
with a neutral message — the UI shows messages verbatim in a toast, so internals
never leak there.

---

## 8. The renderer

Classic scripts, not ES modules: module scripts do not load over `file://`, and
switching to a custom protocol to work around that would add moving parts for no
gain.

```
util.js      esc(), time formatting, DOM helpers
toast.js     transient notifications
api.js       wraps window.aria; every failure becomes a toast
store.js     single source of truth + subscribe/notify
view-*.js    pure render(state) functions, one per panel
modals.js    return · override · confirm (focus-trapped)
actions.js   every state-changing operation
intake.js    audio and text submission
controls.js  the small forms
app.js       bootstrap, SSE, render loop, shortcuts
```

Views never fetch and never call each other. They read `store.state` and redraw.
`app.js` subscribes once and calls every view's `render`.

Timers are the exception: a one-second interval updates only the elements
carrying `data-countdown` / `data-elapsed`, so cards are not re-rendered (and
inputs not blurred) once a second.

**Escaping.** Every value that reaches `innerHTML` goes through `esc()`.
Transcripts and situation labels come from a language model processing a
recording of a stranger's voice; the previous build interpolated them raw.

---

## 9. Testing

96 tests, ~2 s, no models, no network, no Ollama.

| File | Covers |
|---|---|
| `test_priority_queue.py` | Ordering, FIFO ties, upsert, churn, compaction |
| `test_priority_domain.py` | Heap key, escalation, promotion ladder, idempotence |
| `test_inventory.py` | Matching strictness, atomic reservation, consume/restore, capacity |
| `test_rules.py` | Rule routing for 10 report styles, confidence, text normalisation |
| `test_lifecycle.py` | Intake → approve → dispatch → return, cancel, override, escalation, snapshots |
| `test_api.py` | Status codes, error envelope, board consistency |

`conftest.py` sets the offline environment at import time — before pytest
imports the test modules, and therefore before `aria.config` resolves its
settings.

---

## 10. What changed from v1

| Area | Before | Now |
|---|---|---|
| Layout | flat modules, cyclic-ish imports | layered `aria` package, one-way dependencies |
| Types | dicts everywhere, `.get()` chains | pydantic models end to end |
| Heap | full rebuild per tick, linear scan per dispatch | indexed heap, O(log n) |
| Escalation | promotions measured from request time — LOW→CRITICAL in 3 ticks | per-level clocks, 13 h ladder, pure recomputation |
| Inventory | two pandas DataFrames of one file | one ledger, one lock, stdlib csv, atomic writes |
| Consumption | never written off — reserved leaked for ever | `settle_return` restores and consumes |
| Item matching | `partial_ratio ≥ 55` — "kit" matched anything | exact → normalised → fuzzy ≥ 82, else not stocked |
| Async | models called inside `async def` | `run_in_threadpool` + semaphore |
| Config | hard-coded; ignored the env Electron set | `ARIA_*` env + `.env`, legacy `DL_*` honoured |
| Failure | 500s and silent `console.error` | typed errors, one envelope, toasts |
| Startup | embedding model loaded at import | lazy, persisted, fingerprinted index |
| State | lost on exit | atomic snapshots, restored on boot |
| Updates | 3 pollers at 3 s | SSE + full-board responses |
| Frontend | one 1070-line file, unescaped `innerHTML` | 13 modules, escaped throughout |
| Triage without a model | a stub situation | 24-rule cited engine |
| Tests | scripts hitting a live server | 96 offline unit and API tests |
| Dependencies | pandas, APScheduler, requests, llama-cpp | dropped; core install is 3 packages |
