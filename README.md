# ARIA — Autonomous Relief Intelligence Agent

**Offline triage and volunteer dispatch for disaster relief shelters.**
One laptop. No internet. Audio or typed distress reports in; a ranked,
human-approved, inventory-aware dispatch queue out.

---

## The problem

When a disaster hits, connectivity fails first. A shelter's command post is a
laptop on a folding table, and the people running it have:

- a stream of distress reports — phone recordings, radio traffic, runners
- a fixed shelf of supplies and no resupply until the roads reopen
- one to three volunteers who physically walk to whoever needs help
- a binder of first-aid and emergency protocols nobody has time to read

Every report has to be triaged, matched to the right protocol, checked against
what is actually on the shelf, ordered so the most urgent case goes first, and
tracked until the volunteer walks back through the door. ARIA does all of that
locally, and asks a human before it commits anything.

---

## What makes it work offline

ARIA has three triage capabilities and degrades through them rather than
failing:

| Available | What ARIA does |
|---|---|
| Whisper + RAG + local LLM | Full pipeline: denoise → transcribe → retrieve protocols → LLM differential, cross-checked against the rule engine |
| No LLM (Ollama down, no model pulled) | Deterministic **rule engine** produces a cited differential from the same protocol library |
| No models at all — a bare `pip install -r backend/requirements.txt` | Everything above minus audio: typed intake, rule triage, queue, escalation, inventory, dispatch, metrics |

That last row is the point. The core install is ~40 MB and has three
dependencies. A shelter that cannot download 3 GB of model weights still gets a
working triage and dispatch system, and the test suite runs in the same mode —
96 tests, no models, 2 seconds.

---

## Quick start

```bash
# 1. Backend (core only — works immediately)
pip install -r backend/requirements.txt
python backend/main.py

# 2. Desktop app, in another terminal
cd frontend && npm install && npm start
```

The Electron shell starts the backend itself if one is not already running, so
step 2 alone is enough once dependencies are installed.

<details>
<summary><b>Adding speech-to-text and RAG (~3 GB)</b></summary>

```bash
sudo apt install ffmpeg -y                                        # Whisper needs it
pip install torch --index-url https://download.pytorch.org/whl/cpu  # CPU build, not CUDA
pip install -r backend/requirements-ml.txt

# Local LLM, served by Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:1b
```

Nothing else changes: ARIA detects the new capabilities on the next start and
`GET /health/detail` will show them as available.
</details>

<details>
<summary><b>Check what this machine can actually do</b></summary>

```bash
curl -s http://127.0.0.1:8000/health/detail | python3 -m json.tool
```

```json
{
  "status": "degraded",
  "components": [
    {"name": "inventory",       "ok": true,  "detail": "22 item(s) loaded"},
    {"name": "triage_rules",    "ok": true,  "detail": "24 rule(s) loaded"},
    {"name": "protocol_index",  "ok": true,  "detail": "26 document(s) indexed"},
    {"name": "speech_to_text",  "ok": false, "detail": "openai-whisper not installed"},
    {"name": "language_model",  "ok": true,  "detail": "gemma3:1b ready"},
    {"name": "escalation",      "ok": true,  "detail": "running every 60s"},
    {"name": "persistence",     "ok": true,  "detail": "snapshots → backend/state/aria_state.json"}
  ]
}
```

`degraded` means reduced capability, not failure — the dashboard shows a banner
naming exactly what is missing.
</details>

---

## How a report moves through the system

```
   AUDIO FILE                          TYPED REPORT
       │                                     │
       ▼                                     │
  ┌─────────┐  crowd noise, alarms, rain     │
  │ DENOISE │  noisereduce / DNS64 / skip    │
  └────┬────┘                                │
       ▼                                     │
  ┌────────────┐  Whisper base, CPU          │
  │ TRANSCRIBE │  fp16=False                 │
  └────┬───────┘                             │
       └──────────────┬──────────────────────┘
                      ▼
            ┌───────────────────┐  MiniLM embeddings over 26 protocol PDFs
            │ RETRIEVE PROTOCOL │  top-k passages + relevance score
            └────────┬──────────┘
                     │
        relevance < 0.55 ("my uncle isn't moving and his legs look wrong")
                     ▼
            ┌────────────────────┐  LLM or rules propose conditions per
            │ EXPAND HYPOTHESES  │  severity, then retrieve again for each
            └────────┬───────────┘
                     ▼
            ┌────────────────────────────────────────┐
            │ TRIAGE                                 │
            │  LLM differential  ⊎  rule engine      │
            │  merged, deduplicated, severity-ranked │
            └────────┬───────────────────────────────┘
                     ▼
            ┌───────────────────┐  every material checked against the live
            │ CHECK INVENTORY   │  ledger; shortfalls shown, never hidden
            └────────┬──────────┘
                     ▼
    ╔════════════════════════════════════════════════╗
    ║  HUMAN REVIEW — nothing is committed until now  ║
    ║  tick the situations you accept · adjust        ║
    ║  quantities · override entirely · discard       ║
    ╚════════════════┬═══════════════════════════════╝
                     ▼
            ┌───────────────────┐  stock reserved atomically
            │ PRIORITY QUEUE    │  heap_key = severity×1000 − travel×2 − on-site
            └────────┬──────────┘  escalates while it waits
                     ▼
            ┌───────────────────┐  highest-priority request to the first free
            │ DISPATCH          │  volunteer; countdown to expected return
            └────────┬──────────┘
                     ▼
            ┌───────────────────────────────────────┐
            │ BACK AT BASE                          │
            │  returned stock restocked             │
            │  used stock written off               │
            │  volunteer freed → next task assigned │
            └───────────────────────────────────────┘
```

---

## The queue

### Priority key

```
heap_key = severity_score × 1000        CRITICAL 100 · HIGH 75 · MEDIUM 50 · LOW 25
         − travel_time_min × 2
         − resolution_time_min
         + escalation_boost
```

The ×1000 scale makes severity dominate: a CRITICAL case an hour away still
outranks a HIGH case next door. Within one severity tier the time penalties
decide, so of two equally urgent cases the one that can be reached and closed
faster goes first — which serves more people per volunteer-hour.

Ties break by arrival time (first in, first out), so two equally critical
requests never swap places on successive ticks.

### Escalation — nobody starves at the bottom

Every 60 seconds each waiting request is re-evaluated. Two things happen:

- a **boost** inside its own tier, growing with the wait and with how far away
  the casualty is;
- a **promotion** to the next severity label once it has waited too long at the
  current one: LOW → MEDIUM after 6 h, MEDIUM → HIGH after 4 h more, HIGH →
  CRITICAL after 3 h more.

The promotion clock restarts on each promotion, so the full ladder takes 13
hours. Both are recomputed from scratch each pass rather than accumulated, so a
restart, a replay or a double tick can never make a key drift.

*(In the previous build every threshold was measured from the request time, so
a LOW request that passed 6 h also satisfied MEDIUM's and HIGH's thresholds and
was promoted to CRITICAL on three consecutive ticks. There is now a test for
exactly this.)*

---

## The stock ledger

`Total` is the bin's capacity, `Available` is on the shelf, `Reserved` is
committed to an approved request but not yet consumed. The invariant
`Available + Reserved ≤ Total` holds through every operation:

```
reserve   available → reserved     manager approves a situation
release   reserved  → available    request cancelled before dispatch
consume   reserved  → gone         used on site; capacity unchanged
restore   reserved  → available    came back unused
refill              → available    resupply
```

When a volunteer returns, the checklist quantities are restored and **the
difference is written off as consumed**. Without that step — which the previous
build was missing — every partially-used mission left phantom holds behind, and
the shelf filled up with stock that did not exist.

Reservations across several situations are aggregated and applied in one atomic
step, so two situations that both want the last AED cannot each reserve it.
Item names are matched exact → normalised → high-threshold fuzzy, and otherwise
reported as *not stocked* rather than guessed at.

---

## The dashboard

Three columns, dense by design, readable in bad light. Dark, light and
high-contrast themes; every severity carries a label as well as a colour.

**Column 1 — Intake and stock.** Audio upload or typed report. The stock panel
shows available/reserved/capacity as one bar, floats anything low to the top,
and has controls for restocking, adding items and running a resupply.

**Column 2 — Incidents and volunteers.** A metric strip (open · critical · over
SLA · volunteers busy · stock level), filter chips, and the incident cards
themselves — each with severity, status, a live countdown to the volunteer's
expected return, and escalation/vague/degraded markers. Below it, the volunteer
board: who is out, on what, carrying which supplies, and how overdue they are.

**Column 3 — Analysis.** The human-in-the-loop panel. In *review* mode every
hypothesis in the differential is shown with its confidence, which engine
produced it, its reasoning, its steps, its supplies with live stock levels, and
its protocol citations. Tick the ones you accept, adjust quantities, then
approve — or override with your own assessment. *Incident* mode shows the same
for anything on the board plus the full agent hand-off timeline. *Agent log*
mode is the running explainability feed.

Keyboard: `A` audio intake · `T` typed intake · `R` review · `D` incident ·
`L` log · `G` refresh · `/` search stock · `Esc` close dialog ·
`Ctrl+Enter` submit a typed report.

Updates arrive over Server-Sent Events, so the panels move the instant state
changes and always describe the same moment — every mutating endpoint returns
the whole board in one response.

---

## API

Bound to `127.0.0.1`. Interactive docs at `http://127.0.0.1:8000/docs`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/pipeline` | Audio report (base64) → triaged request |
| `POST` | `/pipeline/text` | Typed report → triaged request |
| `GET` | `/board` | Queue + volunteers + inventory + metrics, one snapshot |
| `GET` | `/events` | Server-Sent Events stream of every change |
| `GET` | `/queue`, `/requests`, `/requests/{id}`, `/requests/history` | Request reads |
| `POST` | `/requests/{id}/approve` | Confirm situations, reserve stock, queue |
| `POST` | `/requests/{id}/override` | Replace the AI assessment |
| `POST` | `/requests/{id}/cancel` | Withdraw and release held stock |
| `GET` | `/volunteers` | Roster and live state |
| `POST` | `/volunteers`, `/volunteers/count` | Add a named volunteer / resize |
| `PATCH` | `/volunteers/{id}` | Put on or off shift |
| `DELETE` | `/volunteers/{id}` | Remove from roster |
| `POST` | `/volunteers/{id}/return` | Back at base: settle stock, close request |
| `GET` | `/inventory`, `/inventory/low`, `/inventory/buffer`, `/inventory/history` | Stock reads |
| `POST` | `/inventory`, `/inventory/{item}/stock`, `/inventory/refill` | Stock writes |
| `DELETE` | `/inventory/{item}` | Remove an item |
| `GET` | `/metrics` | Counts, wait times, SLA breaches, utilisation |
| `GET` | `/logs` | Agent hand-off trail |
| `GET` | `/health`, `/health/detail` | Liveness and per-component capability |
| `POST` | `/admin/reload`, `/admin/snapshot`, `/admin/reset` | Maintenance |

Errors always come back in one envelope:

```json
{"error": {"code": "insufficient_stock", "message": "…", "detail": {}}}
```

Full reference: [`docs/API.md`](docs/API.md).

---

## Project layout

```
codeforge_hackathon/
├── backend/
│   ├── main.py                  entry point (--host --port --reload)
│   ├── requirements*.txt        core · ml · dev
│   ├── .env.example             every tunable, documented
│   ├── aria/
│   │   ├── config.py            settings from env + .env
│   │   ├── schemas.py           pydantic contracts (single source of truth)
│   │   ├── domain/              pure logic: severity, heap key, escalation
│   │   ├── core/                errors, logging, indexed heap, event bus
│   │   ├── utils/               time, text matching, audio temp files
│   │   ├── llm/                 pluggable backends: Ollama · OpenVINO · ONNX
│   │   ├── agents/              denoise · transcribe · retrieval · vagueness
│   │   │                        · rules · triage · logistics · pipeline
│   │   ├── services/            inventory · requests · dispatch · escalation
│   │   │                        · persistence · metrics · hub
│   │   └── api/                 FastAPI routes (thin adapters over services)
│   ├── data/
│   │   ├── inventory.csv        the stock ledger
│   │   ├── triage_rules.json    24 rules, ~500 keywords, cited to protocols
│   │   └── protocols/           26 offline first-aid and operations PDFs
│   ├── scripts/                 export_npu_model.py · smoke_test.py
│   └── tests/                   96 offline tests
├── frontend/
│   ├── index.html               three-column shell
│   ├── styles.css               design system, three themes
│   ├── js/                      util · api · store · views · modals · actions
│   └── electron/                main.js (lifecycle) · preload.js (bridge)
└── docs/                        ARCHITECTURE · SETUP · API · CONFIGURATION
```

---

## Development

```bash
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest backend/tests -q          # 96 tests, fully offline, ~2 s
ruff check backend               # lint

python backend/main.py --reload  # auto-reloading dev server
python backend/scripts/smoke_test.py --audio noisy_input   # drive a live server
```

The test suite forces `ARIA_LLM_BACKEND=none`, an empty protocol directory and
a temporary inventory ledger, so it never touches your data, never loads a
model and never reaches the network.

### Configuration

Every tunable is an `ARIA_*` environment variable, documented in
[`backend/.env.example`](backend/.env.example). Useful ones:

```bash
ARIA_LLM_BACKEND=none          # force the deterministic rule engine
ARIA_CONFIDENCE_THRESHOLD=0.55 # below this a report is treated as vague
ARIA_ESCALATION_INTERVAL_SECS=60
ARIA_VOLUNTEER_COUNT=3
ARIA_FUZZY_MIN_SCORE=82        # how strict item-name matching is
ARIA_PERSISTENCE_ENABLED=1     # survive a crash or a flat battery
```

### Adding a triage rule

`backend/data/triage_rules.json` — no code, no restart beyond
`POST /admin/reload`:

```json
{
  "id": "hypothermia",
  "label": "Hypothermia",
  "severity": "HIGH",
  "strong_keywords": ["hypothermia", "very cold", "shivering+blue"],
  "keywords": ["cold", "wet clothes", "confused", "slurring"],
  "travel_time_min": 8,
  "resolution_time_min": 25,
  "materials": [{"item": "Thermal Blanket", "quantity": 2}],
  "instructions": ["Move them somewhere warm and dry", "Remove wet clothing"],
  "protocols": ["QR-05_signs_of_shock.pdf"]
}
```

Matching rules:

- A keyword written with `+` matches when every part appears anywhere in the
  report, in any order — `face+drooping` catches "her face is drooping" as well
  as "face drooping".
- Both the report and the keywords are singularised, so `need blanket` matches
  "she needs blankets".
- Matching is whole-phrase, never substring: `cpr` does not match "reprogram".
- Strong keywords count triple, and a rule needs a score of 2 — one strong
  keyword, or two ordinary ones — before it offers a diagnosis. One incidental
  word never triages anyone.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Desktop shell | Electron | Offline install, native file access, one binary |
| API | FastAPI + Uvicorn | Async, typed, self-documenting, 127.0.0.1 only |
| Contracts | Pydantic v2 | Validation at the boundary; LLM output coerced, never trusted |
| Denoising | noisereduce (DNS64 optional) | CPU-only, fast; optional deep model when quality matters |
| Speech-to-text | openai-whisper `base` | Best offline accuracy per MB on CPU |
| Retrieval | LlamaIndex + all-MiniLM-L6-v2 | Small, fast, good enough for 26 documents; index persisted |
| LLM | Ollama (Gemma 3 1B), OpenVINO/ONNX on NPU | Swappable behind one interface; all optional |
| Deterministic triage | Custom rule engine | Works with nothing installed; fully explainable |
| Queue | Indexed max-heap, lazy deletion | O(log n) update; no rebuild per escalation tick |
| Scheduling | Daemon thread | One less dependency than APScheduler; deterministic shutdown |
| Inventory | stdlib `csv` + atomic writes | A 20-row ledger does not need pandas |
| Live updates | Server-Sent Events | One stream instead of three pollers |
| Persistence | Atomic JSON snapshots | The board survives a crash or a flat battery |

Everything is open source and runs on CPU.

---

## Design decisions worth knowing

**Nothing is committed without a human.** The models rank and propose; the
shelter manager decides. No stock is reserved and no volunteer moves until
someone ticks a box.

**Degradation is a feature, not an error path.** Each capability that is
missing removes exactly what it provides and records why on the request. The
only hard failure is audio intake with no speech model — and that error names
the text endpoint as the way through.

**The volunteer timer never frees anyone automatically.** It counts down, then
counts overdue. Only the shelter head clicking *Back at base* frees a
volunteer, because a timer hitting zero is not evidence that anyone came home.

**Over-triage beats under-triage.** Unparseable severity defaults to HIGH; on
merge conflicts the more severe assessment wins; ties in the rule engine go to
the more severe rule.

**Everything is auditable.** Every agent hand-off, every stock movement and
every decision is recorded with a timestamp and a reason, visible in the UI and
in `backend/logs/`.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layers, data flow, concurrency, state model |
| [`docs/SETUP.md`](docs/SETUP.md) | Full install per platform, models, troubleshooting |
| [`docs/API.md`](docs/API.md) | Every endpoint with request and response examples |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Every setting and how to tune it |

---

## License

MIT — open for humanitarian use and adaptation.
