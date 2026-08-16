# Setup

Install once with an internet connection, then disconnect. ARIA makes no
outbound network calls at runtime.

---

## Requirements

| | Minimum | Comfortable |
|---|---|---|
| RAM | 2 GB (core) · 6 GB (with models) | 8 GB+ |
| Disk | 100 MB (core) · 5 GB (with models) | 10 GB |
| CPU | any x86-64 / ARM64 | 4+ cores |
| Python | 3.10 | 3.11 or 3.12 |
| Node | 18 | 20 LTS |
| OS | Windows 10 · Ubuntu 20.04 · macOS 11 | any |

---

## Tier 1 — core (2 minutes, ~40 MB)

Everything except audio and RAG: typed intake, rule-based triage, the queue,
escalation, inventory, dispatch, metrics and the full dashboard.

```bash
git clone <your-repo-url> && cd codeforge_hackathon

python3 -m venv backend/venv
source backend/venv/bin/activate          # Windows: backend\venv\Scripts\activate
pip install -r backend/requirements.txt

python backend/main.py
```

```bash
# another terminal
cd frontend
npm install
npm start
```

The Electron shell finds `backend/venv` automatically. Verify:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","version":"2.0.0","uptime_secs":3.2}
```

Try it without any models — this returns a full CRITICAL triage from the rule
engine:

```bash
curl -s -X POST http://127.0.0.1:8000/pipeline/text \
     -H 'Content-Type: application/json' \
     -d '{"text":"He collapsed and is not breathing, there is no pulse"}'
```

---

## Tier 2 — speech-to-text and RAG (~3 GB)

```bash
# ffmpeg: Whisper shells out to it for audio decoding
sudo apt install ffmpeg -y          # Debian/Ubuntu
brew install ffmpeg                 # macOS
choco install ffmpeg                # Windows

# torch from the CPU index — the default PyPI wheel drags in ~2.5 GB of CUDA
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r backend/requirements-ml.txt
```

The embedding model (~90 MB) downloads on first start and is then cached in
`~/.cache/huggingface`. Whisper `base` (~140 MB) downloads on the first audio
report and is cached in `~/.cache/whisper`. **Do both before going offline:**

```bash
python -c "import whisper; whisper.load_model('base')"
python -c "from sentence_transformers import SentenceTransformer; \
           SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

The protocol index is built on first start from `backend/data/protocols/` and
persisted to `backend/vector_store/`. Later starts reuse it unless the PDF set
changes (detected by name, size and mtime).

---

## Tier 3 — the local LLM (~1 GB)

```bash
curl -fsSL https://ollama.com/install.sh | sh    # or the macOS/Windows installer
ollama pull gemma3:1b
ollama serve                                     # usually already running
```

ARIA connects on demand and reports the result in `/health/detail`. Any Ollama
model works:

```bash
ARIA_OLLAMA_MODEL=qwen2.5:1.5b python backend/main.py
```

To run deliberately without it: `ARIA_LLM_BACKEND=none`.

---

## Tier 4 — NPU acceleration (optional)

For an Intel Core Ultra NPU or an Apple Neural Engine:

```bash
pip install "optimum[openvino,onnxruntime]" transformers
pip install openvino-genai          # Intel
pip install onnxruntime-genai       # Apple / DirectML

python backend/scripts/export_npu_model.py     # ~2 GB, once
```

Toggle **NPU** in the top bar. If the runtime or the export is missing, ARIA
falls back to Ollama and then to the rule engine — the toggle never breaks a
report.

---

## Going offline

1. Confirm every capability you want is green:
   ```bash
   curl -s http://127.0.0.1:8000/health/detail | python3 -m json.tool
   ```
2. Send one audio report so Whisper is downloaded and cached.
3. Disconnect. Nothing in ARIA calls out.

---

## Your own data

**Inventory** — `backend/data/inventory.csv`. Columns:
`Item,Available,Reserved,Total,Bin Location,Category`. `Total` is capacity; the
ledger repairs impossible rows on load (`Total` is raised to
`Available + Reserved`).

**Protocols** — drop PDFs into `backend/data/protocols/` and run
`curl -X POST 'http://127.0.0.1:8000/admin/reload?rebuild_index=true'`.

**Triage rules** — `backend/data/triage_rules.json`, then
`POST /admin/reload`. Rule format is documented in the main README.

Keeping data on removable media:

```bash
ARIA_INVENTORY_CSV=/media/usb/inventory.csv \
ARIA_PROTOCOLS_DIR=/media/usb/protocols \
ARIA_STATE_FILE=/media/usb/aria_state.json \
python backend/main.py
```

---

## Troubleshooting

**Port 8000 already in use**
```bash
pkill -f "backend/main.py"        # or: python backend/main.py --port 8100
```
Electron reads `ARIA_API_PORT`, so set it for both sides:
`ARIA_API_PORT=8100 npm start`.

**Electron shows the backend-failed dialog** — it prints the last 20 lines of
backend output. Usually a missing dependency or an occupied port. Start the
backend by hand to see the whole traceback.

**Blank window on WSL2 / Wayland** — GPU acceleration is already disabled in
`main.js`. If it persists: `export LIBGL_ALWAYS_SOFTWARE=1`.

**"openai-whisper is not installed"** — expected on a Tier 1 install. Use the
TEXT tab, or install Tier 2.

**Whisper raises about ffmpeg** — install the system package; it is not a Python
dependency.

**"Cannot reach Ollama"** — `ollama serve`, then `ollama list` to confirm the
model is pulled. ARIA keeps working through the rule engine either way.

**Triage feels shallow** — check `/health/detail`. `language_model: false` means
you are seeing rule-engine output, which is correct but less nuanced.

**Everything is flagged VAGUE** — the protocol index is not built (Tier 2 not
installed, or no PDFs). Retrieval returns nothing, so every report scores 0.

**Reserved counts look wrong** — `POST /inventory/refill {"mode":"daily"}`
resets availability and clears every hold. `GET /inventory/history` shows every
movement that led to the current numbers.

**Start clean** — `POST /admin/reset` cancels open requests and deletes the
snapshot. Volunteers already in the field are left alone; bring them back first.

---

## Running the tests

```bash
pip install -r backend/requirements-dev.txt
pytest backend/tests -q            # 96 tests, ~2 s, no models needed
```

Against a live server, including the audio path:

```bash
python backend/scripts/smoke_test.py --audio noisy_input
```
