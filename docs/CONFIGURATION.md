# Configuration

Every tunable is an environment variable. Resolution order, first hit wins:

1. the process environment
2. `backend/.env` (copy from `backend/.env.example`)
3. the defaults below

Settings are resolved once at import. Restart the backend after changing them —
except the inventory CSV and the triage rules, which `POST /admin/reload`
re-reads live.

---

## API

| Variable | Default | Notes |
|---|---|---|
| `ARIA_API_HOST` | `127.0.0.1` | Also accepts the legacy `DL_API_HOST` that Electron sets. **Keep it on localhost** — there is no authentication. |
| `ARIA_API_PORT` | `8000` | Set it for both the backend and Electron. |
| `ARIA_CORS_ORIGINS` | `http://localhost,http://127.0.0.1,file://` | Only relevant if you drive the API from a browser. |
| `ARIA_MAX_UPLOAD_BYTES` | `67108864` (64 MB) | Checked before the base64 payload is decoded. |
| `ARIA_MAX_CONCURRENT_PIPELINES` | `1` | Two CPU inference jobs finish slower together than one after the other. Raise only with cores to spare. |
| `ARIA_SSE_HEARTBEAT_SECS` | `15` | Keep-alive on the event stream. |

---

## Language model

| Variable | Default | Notes |
|---|---|---|
| `ARIA_LLM_BACKEND` | `auto` | `auto` · `ollama` · `npu` · `none`. `none` forces the deterministic rule engine — useful for demos, tests and reproducibility. |
| `ARIA_OLLAMA_URL` | `http://localhost:11434` | |
| `ARIA_OLLAMA_MODEL` | `gemma3:1b` | Any pulled model. Tag drift (`gemma3:1b` vs `gemma3:1b-instruct-q4_0`) is tolerated. |
| `ARIA_LLM_CONTEXT_SIZE` | `8192` | Must match what the model actually supports; the prompt is trimmed to fit. |
| `ARIA_LLM_MAX_TOKENS` | `1200` | Upper bound on the completion. |
| `ARIA_LLM_TEMPERATURE` | `0.15` | Low on purpose: this is triage, not prose. |
| `ARIA_LLM_TIMEOUT_SECS` | `120` | A cold 1B model on a slow CPU can take a while. |
| `ARIA_LLM_HEALTH_TIMEOUT_SECS` | `3` | Health probes must stay cheap. |

If a prompt leaves less than 256 tokens of context, the call is refused rather
than truncated — a half-generated JSON differential parses into a *partial*
situation list, which is worse than none.

---

## Audio

| Variable | Default | Notes |
|---|---|---|
| `ARIA_DENOISER` | `noisereduce` | `noisereduce` (fast) · `facebook` (DNS64, better on non-stationary noise, much slower) · `none`. |
| `ARIA_DENOISE_STRENGTH` | `0.85` | 0–1. Above ~0.9 quiet speech starts to disappear. |
| `ARIA_WHISPER_MODEL` | `base` | `tiny` · `base` · `small` · `medium`. `small` is noticeably better on accented speech and about 3× slower. |
| `ARIA_WHISPER_LANGUAGE` | `en` | Empty string lets Whisper auto-detect (slower). |
| `ARIA_KEEP_TEMP_AUDIO` | `0` | `1` keeps `backend/temp/*.wav` for debugging the denoiser. |
| `ARIA_ACCEPTED_AUDIO_EXTENSIONS` | `.wav,.mp3,.flac,.ogg,.m4a` | Enforced in the UI. |

---

## Retrieval

| Variable | Default | Notes |
|---|---|---|
| `ARIA_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | ~90 MB. Larger models mean a slower first start and a bigger index. |
| `ARIA_RAG_TOP_K` | `5` | Passages retrieved per query. |
| `ARIA_CONFIDENCE_THRESHOLD` | `0.55` | **The dial that matters.** Below this the report is treated as vague and expanded into hypotheses. Too high (the old default was 0.8) and every report is expanded, costing seconds per call; too low and genuinely ambiguous reports go straight to triage with poor context. |
| `ARIA_VAGUENESS_MAX_QUERIES` | `4` | Extra retrievals spent on expansion. Severity-first, so CRITICAL hypotheses always get one. |
| `ARIA_VAGUENESS_TOP_K` | `3` | Passages per hypothesis query. |
| `ARIA_MAX_MERGED_CHUNKS` | `10` | Cap on the merged context. |
| `ARIA_PERSIST_INDEX` | `1` | Saves the index to `backend/vector_store/`, fingerprinted by PDF name/size/mtime. Turning this off costs ~30 s on every start. |

---

## Triage

| Variable | Default | Notes |
|---|---|---|
| `ARIA_TRIAGE_RULES_ENABLED` | `1` | The offline safety net. Disable only to measure the LLM alone. |
| `ARIA_TRIAGE_MERGE_RULES` | `1` | `1` merges rule hypotheses with the model's — broader differential, catches a small model fixating on one diagnosis. `0` uses rules only as a fallback. |
| `ARIA_TRIAGE_MAX_SITUATIONS` | `4` | More than four options is not a differential, it is a menu. |
| `ARIA_TRIAGE_MIN_RULE_CONFIDENCE` | `0.25` | Floor for a rule hypothesis to be offered. |
| `ARIA_DEFAULT_TRAVEL_MIN` | `10` | Used when nothing supplies an estimate. |
| `ARIA_DEFAULT_RESOLUTION_MIN` | `20` | |

---

## Queue and escalation

| Variable | Default | Notes |
|---|---|---|
| `ARIA_SCALE_FACTOR` | `1000` | Multiplies the severity score in the heap key. Lower it and travel time starts to outweigh severity — a distant CRITICAL could sink below a nearby HIGH. Do not go below ~200. |
| `ARIA_ESCALATION_ENABLED` | `1` | |
| `ARIA_ESCALATION_INTERVAL_SECS` | `60` | Must stay far below the promotion delays (hours). |
| `ARIA_SLA_CRITICAL_MIN` | `5` | Response targets used for the SLA counters. |
| `ARIA_SLA_HIGH_MIN` | `30` | |
| `ARIA_SLA_MEDIUM_MIN` | `120` | |
| `ARIA_SLA_LOW_MIN` | `360` | |

Promotion delays (LOW→MEDIUM 6 h, MEDIUM→HIGH 4 h, HIGH→CRITICAL 3 h) and the
boost schedule live in `aria/domain/priority.py`. They are policy, not
configuration: changing them should be a reviewed code change with a test, not
an environment variable someone sets during an incident.

---

## Dispatch

| Variable | Default | Notes |
|---|---|---|
| `ARIA_VOLUNTEER_COUNT` | `3` | Starting roster size. |
| `ARIA_MAX_VOLUNTEERS` | `50` | Guard against a typo in the roster field. |
| `ARIA_AUTO_DISPATCH` | `1` | `0` means approval queues the request but assigns nobody until you act. |

---

## Inventory

| Variable | Default | Notes |
|---|---|---|
| `ARIA_REFILL_THRESHOLD` | `0.60` | "Top up low" refills anything at or below this fraction. |
| `ARIA_LOW_STOCK_THRESHOLD` | `0.20` | Drives the LOW badge and the metric tile. |
| `ARIA_FUZZY_MIN_SCORE` | `82` | **Safety-critical.** Minimum score (0–100) before a fuzzy item-name match is trusted. Lower it and a request for "gloves" can reserve "Glucose Tablets"; the old build's threshold of 55 with `partial_ratio` matched almost anything. Unmatched names are reported as *not stocked* rather than guessed. |
| `ARIA_BUFFER_CAPACITY` | `100` | Overflow store for returned stock that no longer fits its bin. |
| `ARIA_INVENTORY_HISTORY_LIMIT` | `500` | In-memory movement log length. |

---

## Persistence and logging

| Variable | Default | Notes |
|---|---|---|
| `ARIA_PERSISTENCE_ENABLED` | `1` | Atomic JSON snapshots; the board survives a crash or a flat battery. |
| `ARIA_PERSISTENCE_FLUSH_SECS` | `2` | Debounce window. |
| `ARIA_LOG_LEVEL` | `INFO` | `DEBUG` adds prompt sizes and per-chunk retrieval detail. |
| `ARIA_LOG_TO_FILE` | `1` | Rotating `backend/logs/aria.log`, 2 MB × 3. |
| `ARIA_AUDIT_LIMIT` | `1000` | Hand-off entries kept in memory for `GET /logs`; the JSONL file keeps everything. |

---

## Paths

| Variable | Default |
|---|---|
| `ARIA_DATA_DIR` | `backend/data` |
| `ARIA_PROTOCOLS_DIR` | `backend/data/protocols` |
| `ARIA_INVENTORY_CSV` | `backend/data/inventory.csv` |
| `ARIA_TRIAGE_RULES` | `backend/data/triage_rules.json` |
| `ARIA_MODELS_DIR` | `backend/models` |
| `ARIA_VECTOR_STORE` | `backend/vector_store` |
| `ARIA_LOGS_DIR` | `backend/logs` |
| `ARIA_TEMP_DIR` | `backend/temp` |
| `ARIA_STATE_FILE` | `backend/state/aria_state.json` |

Every directory is created on start.

---

## Recipes

**Fully deterministic (demos, tests, reproducible runs)**
```bash
ARIA_LLM_BACKEND=none ARIA_TRIAGE_RULES_ENABLED=1 python backend/main.py
```

**Maximum accuracy, slow hardware acceptable**
```bash
ARIA_WHISPER_MODEL=small ARIA_DENOISER=facebook \
ARIA_OLLAMA_MODEL=qwen2.5:3b ARIA_RAG_TOP_K=8 python backend/main.py
```

**Fastest possible turnaround**
```bash
ARIA_WHISPER_MODEL=tiny ARIA_DENOISER=none \
ARIA_CONFIDENCE_THRESHOLD=0.4 ARIA_VAGUENESS_MAX_QUERIES=2 python backend/main.py
```

**Data on removable media**
```bash
ARIA_INVENTORY_CSV=/media/usb/inventory.csv \
ARIA_PROTOCOLS_DIR=/media/usb/protocols \
ARIA_STATE_FILE=/media/usb/aria_state.json python backend/main.py
```

**Two shelters on one machine**
```bash
ARIA_API_PORT=8000 ARIA_DATA_DIR=/srv/shelter-a python backend/main.py
ARIA_API_PORT=8001 ARIA_DATA_DIR=/srv/shelter-b python backend/main.py
```
