"""NPU backends — OpenVINO (Intel Core Ultra) and ONNX Runtime GenAI (Apple ANE).

Both runtimes are imported lazily inside ``__init__`` so that importing this
module on a machine without them costs nothing.  A missing runtime surfaces as
:class:`AgentUnavailableError`, which the registry turns into a fallback to
Ollama rather than a crash.

Export the model weights first with ``python backend/scripts/export_npu_model.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from aria.config import settings
from aria.core.errors import AgentUnavailableError
from aria.core.logging import get_logger
from aria.llm.base import LLMClient

log = get_logger("llm.npu")


class OpenVINOClient(LLMClient):
    """Intel NPU via ``openvino_genai``.  Expects an int4 IR export."""

    def __init__(self, model_path: Optional[Path] = None, context_size: Optional[int] = None) -> None:
        super().__init__(context_size)
        self.model_path = Path(model_path or settings.paths.models / "openvino")
        self.name = "openvino:npu"
        if not self.model_path.exists():
            raise AgentUnavailableError(
                f"OpenVINO model not found at {self.model_path}", backend=self.name
            )
        try:
            import openvino_genai as ov_genai  # noqa: PLC0415 - intentional lazy import
        except ImportError as exc:
            raise AgentUnavailableError(
                "openvino-genai is not installed", backend=self.name
            ) from exc
        try:
            log.info("Loading OpenVINO pipeline from %s on NPU", self.model_path)
            self._pipe = ov_genai.LLMPipeline(str(self.model_path), "NPU")
            self._genai = ov_genai
        except Exception as exc:  # noqa: BLE001 - driver/compile failures are common
            raise AgentUnavailableError(
                f"Could not initialise the Intel NPU: {exc}", backend=self.name
            ) from exc

    def _generate(self, prompt: str, *, max_tokens: int, temperature: float) -> str:
        config = self._genai.GenerationConfig()
        config.max_new_tokens = max_tokens
        config.temperature = temperature
        return str(self._pipe.generate(prompt, config))

    def health(self) -> tuple[bool, str]:
        return True, f"OpenVINO NPU ready ({self.model_path.name})"


class OnnxClient(LLMClient):
    """Apple Neural Engine / DirectML via ``onnxruntime_genai``."""

    def __init__(self, model_path: Optional[Path] = None, context_size: Optional[int] = None) -> None:
        super().__init__(context_size)
        self.model_path = Path(model_path or settings.paths.models / "onnx")
        self.name = "onnx:ane"
        if not self.model_path.exists():
            raise AgentUnavailableError(
                f"ONNX model not found at {self.model_path}", backend=self.name
            )
        try:
            import onnxruntime_genai as og  # noqa: PLC0415 - intentional lazy import
        except ImportError as exc:
            raise AgentUnavailableError(
                "onnxruntime-genai is not installed", backend=self.name
            ) from exc
        try:
            log.info("Loading ONNX model from %s", self.model_path)
            self._og = og
            self._model = og.Model(str(self.model_path))
            self._tokenizer = og.Tokenizer(self._model)
        except Exception as exc:  # noqa: BLE001
            raise AgentUnavailableError(
                f"Could not initialise ONNX Runtime GenAI: {exc}", backend=self.name
            ) from exc

    def estimate_tokens(self, text: str) -> int:
        try:
            return len(self._tokenizer.encode(text))
        except Exception:  # noqa: BLE001 - fall back to the character heuristic
            return super().estimate_tokens(text)

    def _generate(self, prompt: str, *, max_tokens: int, temperature: float) -> str:
        tokens = self._tokenizer.encode(prompt)
        params = self._og.GeneratorParams(self._model)
        params.set_search_options(
            {"max_length": len(tokens) + max_tokens, "temperature": temperature}
        )
        params.input_ids = tokens
        output: Any = self._model.generate(params)
        generated = output[0][len(tokens) :]
        return str(self._tokenizer.decode(generated))

    def health(self) -> tuple[bool, str]:
        return True, f"ONNX Runtime ready ({self.model_path.name})"
