# ARIA backend

FastAPI service: intake pipeline, triage, priority queue, dispatch, inventory.
Bound to `127.0.0.1`, no outbound network calls at runtime.

```bash
pip install -r requirements.txt        # core, ~40 MB — this alone works
python main.py                         # http://127.0.0.1:8000
python main.py --port 8100 --reload    # development
pytest tests -q                        # 96 tests, offline, ~2 s
```

Full documentation lives in [`../docs/`](../docs/): architecture, setup, API
reference, configuration.

---

## Package map

```
main.py                 entry point; --host --port --reload --log-level
requirements.txt        core (fastapi, uvicorn, pydantic, rapidfuzz)
requirements-ml.txt     whisper, torch, llama-index, sentence-transformers
requirements-dev.txt    pytest, httpx, ruff
.env.example            every tunable, documented

aria/
  config.py             settings from env + .env; nothing imported from it mutates
  schemas.py            pydantic contracts — the single source of truth for payloads

  domain/               pure logic, no I/O, no third-party imports
    enums.py            Severity, RequestStatus, VolunteerStatus
    priority.py         heap key formula, escalation schedule, promotion ladder

  core/                 infrastructure
    errors.py           exception hierarchy → HTTP status codes
    logging.py          logging setup + the agent audit trail
    priority_queue.py   indexed max-heap with lazy deletion
    eventbus.py         in-process pub/sub feeding the SSE stream

  utils/                timeutil · textutil (matching) · audiofile (temp files)

  llm/                  base.py (interface) · ollama.py · npu.py · registry
  agents/               denoise · transcribe · retrieval · vagueness · rules
                        · triage · logistics · pipeline (orchestrator)
  services/             inventory · requests · dispatch · escalation
                        · persistence · metrics · hub (composition root)
  api/                  deps.py + routes/ (thin adapters over services)

data/
  inventory.csv         the stock ledger
  triage_rules.json     24 deterministic rules cited to protocol documents
  protocols/            26 offline first-aid and operations PDFs

scripts/
  export_npu_model.py   one-off model export for Intel NPU / Apple ANE
  smoke_test.py         drive a live server end to end over HTTP

tests/                  96 offline unit and API tests
```

Dependencies point one way: `api → services → agents → domain`. `domain` and
`utils` import nothing beyond the standard library, which is why the test suite
runs with no ML stack installed.

---

## Working with it

**Add an endpoint** — a route in `aria/api/routes/`, registered in
`routes/__init__.py`. Routes validate, call the hub, and shape the response;
business logic belongs in a service.

**Add a pipeline step** — a module in `aria/agents/` that raises
`AgentUnavailableError` when its dependency is missing, then wire it into
`agents/pipeline.py` with a `HandoffLog` entry so it shows up in the timeline.

**Add an LLM backend** — subclass `LLMClient` in `aria/llm/`, implement
`_generate` and `health`, register it in `llm/__init__.py`. The agents do not
change.

**Add a triage rule** — edit `data/triage_rules.json`, then
`POST /admin/reload`. No code, no restart.

**Change escalation policy** — `aria/domain/priority.py`. It is pure and
directly unit-testable; `tests/test_priority_domain.py` is the place to prove
the new behaviour.

---

## Degraded operation

Each capability that is missing removes exactly what it provides:

| Missing | Effect |
|---|---|
| `noisereduce` / `scipy` | raw audio goes to Whisper; request flagged degraded |
| `openai-whisper` / ffmpeg | `POST /pipeline` returns 503 pointing at `/pipeline/text` |
| `llama-index` / embeddings | no protocol search; the rule engine carries triage |
| Ollama or a pulled model | rule engine only; every request flagged degraded |
| `rapidfuzz` | falls back to `difflib` for item matching |

`GET /health/detail` reports each of these by name. The core loop — queue,
escalation, inventory, dispatch — has no optional dependencies at all.
