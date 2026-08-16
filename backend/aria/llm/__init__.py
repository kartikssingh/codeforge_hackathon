"""LLM registry — resolves and caches the model client for this machine.

Selection order for ``ARIA_LLM_BACKEND=auto`` (the default):

1. NPU, when the caller asked for it (the CPU/NPU toggle in the UI) and the
   runtime plus exported weights are actually present.
2. Ollama, when the daemon answers and the configured model is pulled.
3. Nothing — and that is a supported state, not an error.  The triage agent
   falls back to the deterministic rule engine, which is why ARIA still triages
   on a laptop with no models installed at all.

Clients are cached per backend so a 1B model is loaded once per process, not
once per request.
"""

from __future__ import annotations

import threading
from typing import Optional

from aria.config import settings
from aria.core.errors import AgentUnavailableError
from aria.core.logging import get_logger
from aria.llm.base import LLMClient
from aria.llm.ollama import OllamaClient

log = get_logger("llm.registry")

_lock = threading.Lock()
_cache: dict[str, Optional[LLMClient]] = {}


def _build_npu() -> Optional[LLMClient]:
    import platform

    from aria.llm.npu import OnnxClient, OpenVINOClient

    system = platform.system()
    order = [OnnxClient, OpenVINOClient] if system == "Darwin" else [OpenVINOClient, OnnxClient]
    for factory in order:
        try:
            client = factory()
            log.info("NPU backend ready: %s", client.name)
            return client
        except AgentUnavailableError as exc:
            log.info("NPU backend %s unavailable: %s", factory.__name__, exc.message)
        except Exception as exc:  # noqa: BLE001
            log.warning("NPU backend %s failed to load: %s", factory.__name__, exc)
    return None


def _build_ollama() -> Optional[LLMClient]:
    client = OllamaClient()
    ok, detail = client.health()
    if ok:
        log.info("Ollama backend ready: %s", detail)
        return client
    log.warning("Ollama unavailable: %s", detail)
    return None


def get_llm(*, prefer_npu: bool = False, refresh: bool = False) -> Optional[LLMClient]:
    """Return a usable client, or None when this machine has no model."""
    backend = settings.llm.backend.lower()
    if backend == "none":
        return None

    key = "npu" if (prefer_npu and backend in {"auto", "npu"}) else "cpu"
    with _lock:
        if refresh:
            _cache.pop(key, None)
        if key in _cache:
            return _cache[key]

        client: Optional[LLMClient] = None
        if key == "npu":
            client = _build_npu()
            if client is None:
                log.info("Falling back from NPU to the CPU backend")
        if client is None and backend in {"auto", "ollama", "npu"}:
            client = _cache.get("cpu") or _build_ollama()

        _cache[key] = client
        if key == "npu" and client is not None and client.name.startswith("ollama"):
            _cache.setdefault("cpu", client)
        return client


def describe() -> dict[str, object]:
    """Diagnostics for ``/health/detail`` — never raises, never loads a model."""
    client = _cache.get("cpu") or _cache.get("npu")
    if client is None:
        return {
            "backend": settings.llm.backend,
            "loaded": False,
            "detail": "no model loaded yet (loads on first triage)",
        }
    ok, detail = client.health()
    return {"backend": client.name, "loaded": True, "ok": ok, "detail": detail}


def reset() -> None:
    """Drop cached clients — used by tests and by ``POST /admin/reload``."""
    with _lock:
        _cache.clear()


__all__ = ["LLMClient", "OllamaClient", "describe", "get_llm", "reset"]
