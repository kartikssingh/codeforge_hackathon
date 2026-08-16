"""Crash-safe state snapshots.

The old build kept every request, volunteer and reservation in module-level
dicts.  Closing the laptop lid, a Python traceback or a flat battery erased the
entire incident board — during a disaster, with people still waiting.

This service writes a JSON snapshot atomically (temp file + ``os.replace``) and
restores it on boot.  Writes are debounced: services mark the state dirty, a
daemon thread flushes at most every ``flush_interval_secs``, so a burst of
twenty inventory movements costs one write instead of twenty.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from aria.config import settings
from aria.core.logging import get_logger

log = get_logger("persistence")

SNAPSHOT_VERSION = 2


class PersistenceService:
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        enabled: Optional[bool] = None,
        flush_interval_secs: Optional[float] = None,
    ) -> None:
        self._path = Path(path or settings.paths.state_file)
        self._enabled = settings.persistence.enabled if enabled is None else enabled
        self._interval = flush_interval_secs or settings.persistence.flush_interval_secs
        self._providers: dict[str, Any] = {}
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def register(self, name: str, service: Any) -> None:
        """Register a service exposing ``snapshot()`` / ``restore_snapshot()``."""
        self._providers[name] = service

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="aria-persist", daemon=True)
        self._thread.start()
        log.info("State persistence active → %s", self._path)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        if self._enabled:
            self.flush(force=True)

    def mark_dirty(self) -> None:
        self._dirty.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            if self._dirty.is_set():
                self.flush()

    # ── Write / read ──────────────────────────────────────────────────────────

    def flush(self, *, force: bool = False) -> bool:
        if not self._enabled:
            return False
        if not force and not self._dirty.is_set():
            return False
        self._dirty.clear()

        payload: dict[str, Any] = {"version": SNAPSHOT_VERSION}
        for name, service in self._providers.items():
            try:
                payload[name] = service.snapshot()
            except Exception:  # noqa: BLE001 - one bad provider must not stop the rest
                log.exception("Snapshot failed for %s", name)

        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    "w",
                    delete=False,
                    dir=str(self._path.parent),
                    prefix=".state-",
                    suffix=".tmp",
                    encoding="utf-8",
                )
                with handle:
                    json.dump(payload, handle, default=str, ensure_ascii=False)
                os.replace(handle.name, self._path)
                return True
            except OSError as exc:
                log.error("Could not write state snapshot: %s", exc)
                return False

    def load(self) -> dict[str, int]:
        """Restore every registered provider.  Returns per-provider counts."""
        if not self._enabled or not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("State snapshot unreadable (%s) — starting fresh", exc)
            return {}

        version = payload.get("version")
        if version != SNAPSHOT_VERSION:
            log.warning(
                "State snapshot version %s does not match %s — ignoring",
                version,
                SNAPSHOT_VERSION,
            )
            return {}

        restored: dict[str, int] = {}
        for name, service in self._providers.items():
            data = payload.get(name)
            if not isinstance(data, dict):
                continue
            try:
                count = service.restore_snapshot(data)
                restored[name] = int(count or 0)
            except Exception:  # noqa: BLE001
                log.exception("Restore failed for %s", name)
        if restored:
            log.info("Restored state from %s: %s", self._path.name, restored)
        return restored

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover
                log.warning("Could not delete %s: %s", self._path, exc)
