"""Step 2 — speech to text with Whisper.

The model is loaded lazily on the first call and cached for the process
lifetime: loading it at import time made the Electron health-check poll time out
for ~20 s on a cold start, which looked like a hung app.

When Whisper (or ffmpeg) is missing the agent raises
:class:`AgentUnavailableError`.  The route turns that into a 503 that names the
text-intake endpoint, so a dispatcher can still get the report into the system by
typing it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from aria.config import settings
from aria.core.errors import AgentUnavailableError
from aria.core.logging import get_logger

log = get_logger("agents.transcribe")

_model: Any = None
_model_lock = threading.Lock()


def _load_model() -> Any:
    global _model
    with _model_lock:
        if _model is None:
            try:
                import whisper  # noqa: PLC0415 - lazy on purpose
            except ImportError as exc:
                raise AgentUnavailableError(
                    "openai-whisper is not installed — use POST /pipeline/text instead",
                    component="whisper",
                ) from exc
            log.info("Loading Whisper '%s' (first call only)…", settings.audio.whisper_model)
            try:
                _model = whisper.load_model(settings.audio.whisper_model)
            except Exception as exc:  # noqa: BLE001 - missing weights, bad name…
                raise AgentUnavailableError(
                    f"Could not load Whisper model '{settings.audio.whisper_model}': {exc}",
                    component="whisper",
                ) from exc
            log.info("Whisper ready")
        return _model


def transcribe(audio_path: str | Path) -> str:
    """Return the transcript of a clean ``.wav``.  Raises when unavailable."""
    path = Path(audio_path)
    if not path.exists():
        raise AgentUnavailableError(f"Audio file {path} disappeared", component="whisper")

    model = _load_model()
    try:
        result = model.transcribe(
            str(path),
            fp16=False,  # CPU-only: fp16 raises on machines without a GPU
            language=settings.audio.whisper_language or None,
        )
    except Exception as exc:  # noqa: BLE001 - ffmpeg missing shows up here
        raise AgentUnavailableError(
            f"Transcription failed: {exc}. Is ffmpeg installed?", component="whisper"
        ) from exc

    text = str(result.get("text", "")).strip()
    if not text:
        raise AgentUnavailableError(
            "Whisper returned an empty transcript — the recording may be silent",
            component="whisper",
        )
    return text


def is_available() -> tuple[bool, str]:
    """Cheap probe for ``/health/detail`` that never loads the weights."""
    try:
        import whisper  # noqa: PLC0415, F401
    except ImportError:
        return False, "openai-whisper not installed"
    return True, (
        f"model '{settings.audio.whisper_model}' loaded"
        if _model is not None
        else f"model '{settings.audio.whisper_model}' loads on first use"
    )


def reset() -> None:
    """Release the cached model (tests, and ``POST /admin/reload``)."""
    global _model
    with _model_lock:
        _model = None


def preload() -> Optional[str]:
    """Warm the model up.  Returns an error string instead of raising."""
    try:
        _load_model()
        return None
    except AgentUnavailableError as exc:
        return exc.message
