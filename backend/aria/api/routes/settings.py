"""Runtime configuration for the renderer.

The dashboard should never hard-code a threshold the backend also knows about —
that is how a UI ends up claiming "CRITICAL LOW" at 20 % while the server
refills at 60 %.  Everything the renderer needs to stay consistent with server
behaviour is served from here at boot.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from aria import __version__
from aria.config import settings as cfg
from aria.domain.enums import RequestStatus, Severity

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/frontend")
async def frontend_settings() -> dict[str, Any]:
    return {
        "version": __version__,
        "polling": {
            # SSE is the primary channel; polling is the fallback when the
            # stream drops, so it is deliberately slower than the old 3 s.
            "board_ms": 10000,
            "timers_ms": 1000,
            "reconnect_ms": 3000,
        },
        "audio": {
            "accepted_extensions": list(cfg.audio.accepted_extensions),
            "max_upload_bytes": cfg.api.max_upload_bytes,
        },
        "thresholds": {
            "low_stock_pct": round(cfg.inventory.low_stock_threshold * 100),
            "refill_pct": round(cfg.inventory.refill_threshold * 100),
            "confidence": cfg.rag.confidence_threshold,
        },
        "sla_minutes": {s.value: cfg.sla_minutes(s.value) for s in Severity},
        "severities": [s.value for s in Severity],
        "statuses": [s.value for s in RequestStatus],
        "dispatch": {
            "default_volunteer_count": cfg.dispatch.volunteer_count,
            "max_volunteers": cfg.dispatch.max_volunteers,
            "auto_dispatch": cfg.dispatch.auto_dispatch,
        },
        "ui_text": {
            "upload": {
                "drop_hint": "Click to upload or drag & drop a distress recording",
                "invalid_file": "Unsupported file. Drop a .wav, .mp3, .flac, .ogg or .m4a.",
                "no_file_selected": "Select an audio file first, or type the report instead.",
            },
            "processing": {
                "starting": "Denoising audio…",
                "steps": [
                    "Transcribing speech…",
                    "Searching offline protocols…",
                    "Running triage…",
                    "Checking inventory…",
                ],
                "done": "Triage complete — review and approve.",
            },
        },
    }
