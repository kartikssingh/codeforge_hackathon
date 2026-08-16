"""Logging setup and the agent handoff audit trail.

Two distinct concerns live here:

* :func:`setup_logging` configures ordinary Python logging (console + rotating
  file).  The old build used bare ``print()`` calls scattered across the agents,
  which meant no levels, no timestamps and no way to quieten them.
* :class:`AuditTrail` records structured agent-to-agent handoffs.  These are the
  explainability record the dashboard renders as a timeline and the reason the
  system can answer "why did it decide that?" after the fact.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Optional

from aria.config import settings
from aria.utils.timeutil import now

_CONFIGURED = False
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: Optional[str] = None) -> None:
    """Configure root logging once.  Safe to call repeatedly."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved = (level or settings.observability.log_level).upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved, logging.INFO))

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(console)

    if settings.observability.log_to_file:
        try:
            settings.paths.logs.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                settings.paths.logs / "aria.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
            )
            root.addHandler(file_handler)
        except OSError:  # read-only media, USB stick pulled out, …
            root.warning("File logging disabled: log directory is not writable")

    # These libraries are chatty at INFO and drown out the pipeline trace.
    for noisy in ("httpx", "urllib3", "sentence_transformers", "llama_index", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"aria.{name}" if not name.startswith("aria") else name)


class AuditTrail:
    """Thread-safe ring buffer of handoff events, mirrored to a JSONL file.

    The in-memory buffer is what ``GET /logs`` serves (fast, bounded); the file
    is the durable record an incident review would read afterwards.
    """

    def __init__(self, path: Optional[Path] = None, limit: Optional[int] = None) -> None:
        self._path = path or (settings.paths.logs / "handoffs.jsonl")
        self._entries: deque[dict[str, Any]] = deque(
            maxlen=limit or settings.observability.audit_limit
        )
        self._lock = threading.Lock()
        self._log = get_logger("audit")

    def record(
        self,
        *,
        from_agent: str,
        to_agent: str,
        reason: str = "",
        request_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        **detail: Any,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "at": now().isoformat(),
            "from_agent": from_agent.upper(),
            "to_agent": to_agent.upper(),
            "reason": reason,
            "request_id": request_id,
            "duration_ms": duration_ms,
            "detail": _sanitise(detail),
        }
        with self._lock:
            self._entries.append(entry)
            self._append_to_file(entry)
        self._log.debug("%s → %s (%s)", entry["from_agent"], entry["to_agent"], reason)
        return entry

    def recent(self, limit: int = 100, request_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            entries: Iterable[dict[str, Any]] = list(self._entries)
        if request_id:
            entries = [e for e in entries if e.get("request_id") == request_id]
        return list(entries)[-limit:][::-1]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _append_to_file(self, entry: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            # Losing the durable copy must never break a live dispatch.
            self._log.warning("Could not append to %s", self._path)


def _sanitise(payload: dict[str, Any], *, max_chars: int = 400) -> dict[str, Any]:
    """Trim long blobs so the audit file stays readable and bounded."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > max_chars:
            clean[key] = value[:max_chars] + "…"
        elif isinstance(value, datetime):
            clean[key] = value.isoformat()
        else:
            clean[key] = value
    return clean


#: Process-wide trail.  Injected explicitly where practical, imported directly
#: by the agents, which have no other reason to know about the service layer.
audit = AuditTrail()
