"""LLM client interface.

Every backend (Ollama, OpenVINO NPU, ONNX/ANE) implements the same three
methods, so the triage and vagueness agents never learn which one they are
talking to.  Adding a fourth runtime means adding one file, not touching the
agents.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from aria.config import settings
from aria.core.errors import AgentUnavailableError
from aria.core.logging import get_logger

log = get_logger("llm")

#: Never call a model with less headroom than this — a truncated JSON report is
#: worse than no report, because it parses into a *partial* situation list.
MIN_GENERATION_TOKENS = 256


class LLMClient(ABC):
    """A text-completion model reachable from this machine."""

    #: Human-readable identifier shown in ``/health/detail``.
    name: str = "llm"

    def __init__(self, context_size: Optional[int] = None) -> None:
        self.context_size = context_size or settings.llm.context_size

    # ── Contract ──────────────────────────────────────────────────────────────

    @abstractmethod
    def _generate(self, prompt: str, *, max_tokens: int, temperature: float) -> str:
        """Backend-specific generation.  May raise anything."""

    @abstractmethod
    def health(self) -> tuple[bool, str]:
        """``(reachable, detail)`` — cheap enough to call from a health probe."""

    # ── Shared behaviour ──────────────────────────────────────────────────────

    def estimate_tokens(self, text: str) -> int:
        """Cheap upper-bound estimate (~4 characters per token).

        Deliberately approximate: every backend that can tokenise exactly
        overrides this, and over-estimating only costs a little context.
        """
        return max(1, len(text) // 4 + 1)

    def budget(self, prompt: str, requested: Optional[int] = None) -> int:
        """Completion tokens that still fit in the context window."""
        reserve = 32  # BOS/EOS and template separators
        wanted = requested or settings.llm.max_tokens
        remaining = self.context_size - self.estimate_tokens(prompt) - reserve
        return max(0, min(wanted, remaining))

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Generate text, or raise :class:`AgentUnavailableError`.

        Callers treat the exception as "run without the model" rather than as a
        failure — that is what keeps the pipeline working offline.
        """
        budget = self.budget(prompt, max_tokens)
        if budget < MIN_GENERATION_TOKENS:
            raise AgentUnavailableError(
                f"Prompt leaves only {budget} tokens of context for {self.name}",
                backend=self.name,
            )
        try:
            return self._generate(
                prompt,
                max_tokens=budget,
                temperature=(
                    settings.llm.temperature if temperature is None else temperature
                ),
            ).strip()
        except AgentUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise every backend failure
            raise AgentUnavailableError(
                f"{self.name} generation failed: {exc}", backend=self.name
            ) from exc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(name={self.name!r}, ctx={self.context_size})"
