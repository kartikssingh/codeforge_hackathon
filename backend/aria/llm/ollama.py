"""Ollama backend — the default local LLM runtime.

Talks to Ollama's HTTP API with :mod:`urllib` from the standard library rather
than ``requests``: one fewer package to install on a machine that may never see
the internet again, and the payloads involved are trivial.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

from aria.config import settings
from aria.core.logging import get_logger
from aria.llm.base import LLMClient

log = get_logger("llm.ollama")


class OllamaClient(LLMClient):
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        context_size: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> None:
        super().__init__(context_size or settings.llm.context_size)
        self.base_url = (base_url or settings.llm.ollama_url).rstrip("/")
        self.model = model or settings.llm.ollama_model
        self.timeout = timeout or settings.llm.request_timeout_secs
        self.name = f"ollama:{self.model}"

    # ── Generation ────────────────────────────────────────────────────────────

    def _generate(self, prompt: str, *, max_tokens: int, temperature: float) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "num_ctx": self.context_size,
                "temperature": temperature,
            },
        }
        data = self._post("/api/generate", payload, timeout=self.timeout)
        return str(data.get("response", ""))

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> tuple[bool, str]:
        try:
            data = self._get("/api/tags", timeout=settings.llm.health_timeout_secs)
        except Exception as exc:  # noqa: BLE001
            return False, f"unreachable at {self.base_url} ({exc})"

        installed = [str(m.get("name", "")) for m in data.get("models", [])]
        if not installed:
            return False, f"{self.base_url} has no models pulled"
        if self.model in installed:
            return True, f"{self.model} ready"
        # Tolerate "gemma3:1b" vs "gemma3:1b-instruct-q4_0" style tag drift.
        stem = self.model.split(":", 1)[0]
        near = [name for name in installed if name.split(":", 1)[0] == stem]
        if near:
            return True, f"{near[0]} ready (requested {self.model})"
        return False, f"{self.model} not pulled — available: {', '.join(installed[:5])}"

    # ── HTTP plumbing ─────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def _get(self, path: str, *, timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
