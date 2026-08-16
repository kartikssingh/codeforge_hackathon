"""Central configuration for ARIA.

Every tunable lives here.  Values are resolved once at import time in this
order (first hit wins):

    1. Real process environment variables
    2. ``backend/.env`` (simple ``KEY=VALUE`` file, see ``.env.example``)
    3. The defaults declared below

Naming convention: ``ARIA_<SECTION>_<NAME>``.  A few legacy ``DL_*`` names are
still honoured because ``frontend/electron/main.js`` sets them when it spawns
the backend.

Nothing in this module imports third-party packages, so it is safe to import
from tests that run without the ML stack installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
ENV_FILE = BACKEND_DIR / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` env file.  Never raises."""
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Strip one layer of matching quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_FILE_ENV = _load_env_file(ENV_FILE)


def _raw(*names: str) -> str | None:
    """Return the first value found for *names* in env, then in ``.env``."""
    for name in names:
        if name in os.environ:
            return os.environ[name]
    for name in names:
        if name in _FILE_ENV:
            return _FILE_ENV[name]
    return None


def _str(default: str, *names: str) -> str:
    value = _raw(*names)
    return default if value is None else value


def _int(default: int, *names: str) -> int:
    value = _raw(*names)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float(default: float, *names: str) -> float:
    value = _raw(*names)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _bool(default: bool, *names: str) -> bool:
    value = _raw(*names)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(default: Path, *names: str) -> Path:
    value = _raw(*names)
    return default if value is None else Path(value).expanduser().resolve()


def _csv(default: tuple[str, ...], *names: str) -> tuple[str, ...]:
    value = _raw(*names)
    if value is None:
        return default
    parts = tuple(p.strip() for p in value.split(",") if p.strip())
    return parts or default


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Paths:
    backend: Path = BACKEND_DIR
    project_root: Path = PROJECT_ROOT
    data: Path = field(default_factory=lambda: _path(BACKEND_DIR / "data", "ARIA_DATA_DIR"))
    protocols: Path = field(default_factory=lambda: _path(BACKEND_DIR / "data" / "protocols", "ARIA_PROTOCOLS_DIR"))
    inventory_csv: Path = field(default_factory=lambda: _path(BACKEND_DIR / "data" / "inventory.csv", "ARIA_INVENTORY_CSV"))
    triage_rules: Path = field(default_factory=lambda: _path(BACKEND_DIR / "data" / "triage_rules.json", "ARIA_TRIAGE_RULES"))
    models: Path = field(default_factory=lambda: _path(BACKEND_DIR / "models", "ARIA_MODELS_DIR"))
    vector_store: Path = field(default_factory=lambda: _path(BACKEND_DIR / "vector_store", "ARIA_VECTOR_STORE"))
    logs: Path = field(default_factory=lambda: _path(BACKEND_DIR / "logs", "ARIA_LOGS_DIR"))
    temp: Path = field(default_factory=lambda: _path(BACKEND_DIR / "temp", "ARIA_TEMP_DIR"))
    state_file: Path = field(default_factory=lambda: _path(BACKEND_DIR / "state" / "aria_state.json", "ARIA_STATE_FILE"))

    def ensure(self) -> None:
        """Create every runtime directory.  Idempotent."""
        for directory in (
            self.data,
            self.protocols,
            self.models,
            self.vector_store,
            self.logs,
            self.temp,
            self.state_file.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ApiSettings:
    host: str = field(default_factory=lambda: _str("127.0.0.1", "ARIA_API_HOST", "DL_API_HOST"))
    port: int = field(default_factory=lambda: _int(8000, "ARIA_API_PORT", "DL_API_PORT"))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _csv(
            ("http://localhost", "http://127.0.0.1", "file://"),
            "ARIA_CORS_ORIGINS",
        )
    )
    # Hard ceiling on a single /pipeline upload (base64 payloads inflate ~33%).
    max_upload_bytes: int = field(default_factory=lambda: _int(64 * 1024 * 1024, "ARIA_MAX_UPLOAD_BYTES"))
    # Heavy pipeline runs are serialised so CPU inference cannot thrash.
    max_concurrent_pipelines: int = field(default_factory=lambda: _int(1, "ARIA_MAX_CONCURRENT_PIPELINES"))
    sse_heartbeat_secs: float = field(default_factory=lambda: _float(15.0, "ARIA_SSE_HEARTBEAT_SECS"))


@dataclass(frozen=True)
class AudioSettings:
    # "noisereduce" (fast spectral subtraction) | "facebook" (DNS64) | "none"
    denoiser: str = field(default_factory=lambda: _str("noisereduce", "ARIA_DENOISER", "DENOISER"))
    denoise_strength: float = field(default_factory=lambda: _float(0.85, "ARIA_DENOISE_STRENGTH"))
    whisper_model: str = field(default_factory=lambda: _str("base", "ARIA_WHISPER_MODEL", "WHISPER_MODEL"))
    whisper_language: str = field(default_factory=lambda: _str("en", "ARIA_WHISPER_LANGUAGE"))
    keep_temp_audio: bool = field(default_factory=lambda: _bool(False, "ARIA_KEEP_TEMP_AUDIO"))
    accepted_extensions: tuple[str, ...] = field(
        default_factory=lambda: _csv(
            (".wav", ".mp3", ".flac", ".ogg", ".m4a"),
            "ARIA_ACCEPTED_AUDIO_EXTENSIONS",
        )
    )


@dataclass(frozen=True)
class RagSettings:
    embed_model: str = field(default_factory=lambda: _str("sentence-transformers/all-MiniLM-L6-v2", "ARIA_EMBED_MODEL", "EMBED_MODEL"))
    top_k: int = field(default_factory=lambda: _int(5, "ARIA_RAG_TOP_K", "RAG_TOP_K"))
    # Top chunk score below this ⇒ the report is treated as vague.
    confidence_threshold: float = field(default_factory=lambda: _float(0.55, "ARIA_CONFIDENCE_THRESHOLD", "CONFIDENCE_THRESHOLD"))
    # Extra retrieval passes spent expanding a vague report.
    vagueness_max_queries: int = field(default_factory=lambda: _int(4, "ARIA_VAGUENESS_MAX_QUERIES"))
    vagueness_top_k: int = field(default_factory=lambda: _int(3, "ARIA_VAGUENESS_TOP_K"))
    max_merged_chunks: int = field(default_factory=lambda: _int(10, "ARIA_MAX_MERGED_CHUNKS"))
    persist_index: bool = field(default_factory=lambda: _bool(True, "ARIA_PERSIST_INDEX"))


@dataclass(frozen=True)
class LlmSettings:
    # auto | ollama | npu | none
    backend: str = field(default_factory=lambda: _str("auto", "ARIA_LLM_BACKEND"))
    ollama_url: str = field(default_factory=lambda: _str("http://localhost:11434", "ARIA_OLLAMA_URL", "OLLAMA_URL"))
    ollama_model: str = field(default_factory=lambda: _str("gemma3:1b", "ARIA_OLLAMA_MODEL", "OLLAMA_MODEL"))
    context_size: int = field(default_factory=lambda: _int(8192, "ARIA_LLM_CONTEXT_SIZE", "LLM_CONTEXT_SIZE"))
    max_tokens: int = field(default_factory=lambda: _int(1200, "ARIA_LLM_MAX_TOKENS", "LLM_MAX_TOKENS"))
    temperature: float = field(default_factory=lambda: _float(0.15, "ARIA_LLM_TEMPERATURE", "LLM_TEMPERATURE"))
    request_timeout_secs: float = field(default_factory=lambda: _float(120.0, "ARIA_LLM_TIMEOUT_SECS"))
    health_timeout_secs: float = field(default_factory=lambda: _float(3.0, "ARIA_LLM_HEALTH_TIMEOUT_SECS"))


@dataclass(frozen=True)
class TriageSettings:
    # The deterministic keyword engine always runs; it is the offline safety net.
    rules_enabled: bool = field(default_factory=lambda: _bool(True, "ARIA_TRIAGE_RULES_ENABLED"))
    # Merge rule hypotheses into LLM output instead of only using them as fallback.
    merge_rules_with_llm: bool = field(default_factory=lambda: _bool(True, "ARIA_TRIAGE_MERGE_RULES"))
    max_situations: int = field(default_factory=lambda: _int(4, "ARIA_TRIAGE_MAX_SITUATIONS"))
    min_rule_confidence: float = field(default_factory=lambda: _float(0.25, "ARIA_TRIAGE_MIN_RULE_CONFIDENCE"))
    default_travel_time_min: int = field(default_factory=lambda: _int(10, "ARIA_DEFAULT_TRAVEL_MIN"))
    default_resolution_time_min: int = field(default_factory=lambda: _int(20, "ARIA_DEFAULT_RESOLUTION_MIN"))


@dataclass(frozen=True)
class QueueSettings:
    # Severity dominates the key; travel/resolution only break ties inside a tier.
    scale_factor: int = field(default_factory=lambda: _int(1000, "ARIA_SCALE_FACTOR", "SCALE_FACTOR"))
    escalation_interval_secs: int = field(default_factory=lambda: _int(60, "ARIA_ESCALATION_INTERVAL_SECS", "ESCALATION_INTERVAL_SECS"))
    escalation_enabled: bool = field(default_factory=lambda: _bool(True, "ARIA_ESCALATION_ENABLED"))
    # Minutes a request may wait before it counts as an SLA breach, per severity.
    sla_minutes_critical: int = field(default_factory=lambda: _int(5, "ARIA_SLA_CRITICAL_MIN"))
    sla_minutes_high: int = field(default_factory=lambda: _int(30, "ARIA_SLA_HIGH_MIN"))
    sla_minutes_medium: int = field(default_factory=lambda: _int(120, "ARIA_SLA_MEDIUM_MIN"))
    sla_minutes_low: int = field(default_factory=lambda: _int(360, "ARIA_SLA_LOW_MIN"))


@dataclass(frozen=True)
class DispatchSettings:
    volunteer_count: int = field(default_factory=lambda: _int(3, "ARIA_VOLUNTEER_COUNT", "VOLUNTEER_COUNT"))
    max_volunteers: int = field(default_factory=lambda: _int(50, "ARIA_MAX_VOLUNTEERS"))
    auto_dispatch: bool = field(default_factory=lambda: _bool(True, "ARIA_AUTO_DISPATCH"))


@dataclass(frozen=True)
class InventorySettings:
    refill_threshold: float = field(default_factory=lambda: _float(0.60, "ARIA_REFILL_THRESHOLD"))
    low_stock_threshold: float = field(default_factory=lambda: _float(0.20, "ARIA_LOW_STOCK_THRESHOLD"))
    # Minimum rapidfuzz/difflib score (0-100) before a fuzzy item match is trusted.
    fuzzy_min_score: int = field(default_factory=lambda: _int(82, "ARIA_FUZZY_MIN_SCORE"))
    buffer_default_capacity: int = field(default_factory=lambda: _int(100, "ARIA_BUFFER_CAPACITY"))
    history_limit: int = field(default_factory=lambda: _int(500, "ARIA_INVENTORY_HISTORY_LIMIT"))


@dataclass(frozen=True)
class ObservabilitySettings:
    log_level: str = field(default_factory=lambda: _str("INFO", "ARIA_LOG_LEVEL"))
    log_to_file: bool = field(default_factory=lambda: _bool(True, "ARIA_LOG_TO_FILE"))
    audit_limit: int = field(default_factory=lambda: _int(1000, "ARIA_AUDIT_LIMIT"))


@dataclass(frozen=True)
class PersistenceSettings:
    enabled: bool = field(default_factory=lambda: _bool(True, "ARIA_PERSISTENCE_ENABLED"))
    flush_interval_secs: float = field(default_factory=lambda: _float(2.0, "ARIA_PERSISTENCE_FLUSH_SECS"))


@dataclass(frozen=True)
class Settings:
    paths: Paths = field(default_factory=Paths)
    api: ApiSettings = field(default_factory=ApiSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    rag: RagSettings = field(default_factory=RagSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    triage: TriageSettings = field(default_factory=TriageSettings)
    queue: QueueSettings = field(default_factory=QueueSettings)
    dispatch: DispatchSettings = field(default_factory=DispatchSettings)
    inventory: InventorySettings = field(default_factory=InventorySettings)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)
    persistence: PersistenceSettings = field(default_factory=PersistenceSettings)

    def sla_minutes(self, severity: str) -> int:
        return {
            "CRITICAL": self.queue.sla_minutes_critical,
            "HIGH": self.queue.sla_minutes_high,
            "MEDIUM": self.queue.sla_minutes_medium,
            "LOW": self.queue.sla_minutes_low,
        }.get(severity.upper(), self.queue.sla_minutes_low)


settings = Settings()
settings.paths.ensure()


def reload_settings() -> Settings:
    """Re-read the environment.  Used by tests; the app uses ``settings``."""
    global settings, _FILE_ENV
    _FILE_ENV = _load_env_file(ENV_FILE)
    settings = Settings()
    settings.paths.ensure()
    return settings
