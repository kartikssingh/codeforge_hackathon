"""Step 3 — RAG retrieval over the offline protocol library.

Embeds the report and pulls the most relevant passages out of the PDFs in
``data/protocols/`` using LlamaIndex + a local MiniLM embedding model.

Two changes matter here versus the previous version:

* **Nothing loads at import time.**  The embedding model used to be constructed
  as a module-level side effect, so merely importing the agent downloaded and
  loaded a transformer — including inside unit tests.
* **The index is persisted and fingerprinted.**  Re-embedding 26 PDFs on every
  boot cost ~30 s of a cold start; now the index is rebuilt only when the PDF
  set actually changes.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aria.config import settings
from aria.core.logging import get_logger

log = get_logger("agents.retrieval")

_index: Any = None
_index_lock = threading.RLock()
_index_error: Optional[str] = None
_embeddings_ready = False

_FINGERPRINT_FILE = "source_fingerprint.json"


@dataclass
class Chunk:
    text: str
    score: float
    source: str
    page: str = "?"
    hypothesis: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.source} p.{self.page}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "score": self.score,
            "source": self.source,
            "page": self.page,
            "hypothesis": self.hypothesis,
        }


@dataclass
class RetrievalResult:
    chunks: list[Chunk] = field(default_factory=list)
    top_score: float = 0.0
    is_vague: bool = True
    available: bool = False
    note: str = ""

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.label not in seen:
                seen.append(chunk.label)
        return seen


# ── Index construction ────────────────────────────────────────────────────────


def _fingerprint(pdf_dir: Path) -> dict[str, list[float]]:
    """Cheap change detector: name → [size, mtime] for every source file."""
    prints: dict[str, list[float]] = {}
    for path in sorted(pdf_dir.glob("**/*")):
        if path.is_file() and path.suffix.lower() in {".pdf", ".txt", ".md"}:
            stat = path.stat()
            prints[path.name] = [float(stat.st_size), round(stat.st_mtime, 3)]
    return prints


def _configure_llama_settings() -> None:
    """Load the embedding model once per process (it costs seconds and ~90 MB)."""
    global _embeddings_ready
    if _embeddings_ready:
        return

    from llama_index.core import Settings as LlamaSettings  # noqa: PLC0415
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding  # noqa: PLC0415

    log.info("Loading embedding model %s", settings.rag.embed_model)
    LlamaSettings.embed_model = HuggingFaceEmbedding(model_name=settings.rag.embed_model)
    # Retrieval only — synthesis is handled by our own triage agent.
    LlamaSettings.llm = None
    _embeddings_ready = True


def build_index(pdf_dir: Optional[Path] = None, *, force: bool = False) -> bool:
    """Build or load the vector index.  Returns True when one is available."""
    global _index, _index_error

    directory = Path(pdf_dir or settings.paths.protocols)
    store = Path(settings.paths.vector_store)

    with _index_lock:
        sources = _fingerprint(directory)
        if not sources:
            _index = None
            _index_error = f"no protocol documents in {directory}"
            log.warning("RAG disabled: %s", _index_error)
            return False

        try:
            _configure_llama_settings()
            from llama_index.core import (  # noqa: PLC0415
                SimpleDirectoryReader,
                StorageContext,
                VectorStoreIndex,
                load_index_from_storage,
            )
        except ImportError as exc:
            _index = None
            _index_error = f"llama-index is not installed ({exc})"
            log.warning("RAG disabled: %s", _index_error)
            return False

        fingerprint_path = store / _FINGERPRINT_FILE
        reusable = False
        if settings.rag.persist_index and not force and fingerprint_path.exists():
            try:
                reusable = json.loads(fingerprint_path.read_text(encoding="utf-8")) == sources
            except (OSError, json.JSONDecodeError):
                reusable = False

        try:
            if reusable:
                log.info("Reusing persisted vector index at %s", store)
                _index = load_index_from_storage(
                    StorageContext.from_defaults(persist_dir=str(store))
                )
            else:
                log.info("Indexing %d protocol document(s) from %s…", len(sources), directory)
                documents = SimpleDirectoryReader(str(directory)).load_data()
                _index = VectorStoreIndex.from_documents(documents)
                if settings.rag.persist_index:
                    store.mkdir(parents=True, exist_ok=True)
                    _index.storage_context.persist(persist_dir=str(store))
                    fingerprint_path.write_text(json.dumps(sources), encoding="utf-8")
                log.info("Vector index ready (%d documents)", len(documents))
            _index_error = None
            return True
        except Exception as exc:  # noqa: BLE001 - corrupt store, OOM, bad PDF…
            _index = None
            _index_error = str(exc)
            log.error("Could not build the vector index: %s", exc)
            return False


def retrieve(query: str, top_k: Optional[int] = None) -> RetrievalResult:
    """Semantic search over the protocol library.

    Never raises: an unavailable index degrades to an empty, explicitly vague
    result so the pipeline can continue with the rule engine.
    """
    text = (query or "").strip()
    if not text:
        return RetrievalResult(note="empty query")

    with _index_lock:
        index = _index
        error = _index_error

    if index is None:
        return RetrievalResult(available=False, note=error or "index not built")

    try:
        retriever = index.as_retriever(similarity_top_k=top_k or settings.rag.top_k)
        nodes = retriever.retrieve(text)
    except Exception as exc:  # noqa: BLE001
        log.error("Retrieval failed: %s", exc)
        return RetrievalResult(available=False, note=str(exc))

    chunks = [
        Chunk(
            text=node.node.get_content(),
            score=float(node.score or 0.0),
            source=str(node.node.metadata.get("file_name", "unknown")),
            page=str(node.node.metadata.get("page_label", "?")),
        )
        for node in nodes
    ]
    top_score = chunks[0].score if chunks else 0.0
    return RetrievalResult(
        chunks=chunks,
        top_score=top_score,
        is_vague=top_score < settings.rag.confidence_threshold,
        available=True,
    )


def status() -> dict[str, Any]:
    with _index_lock:
        return {
            "ready": _index is not None,
            "error": _index_error,
            "protocols_dir": str(settings.paths.protocols),
            "documents": len(_fingerprint(Path(settings.paths.protocols))),
        }


def reset() -> None:
    """Drop the in-memory index (tests, ``POST /admin/reload``)."""
    global _index, _index_error
    with _index_lock:
        _index = None
        _index_error = None
