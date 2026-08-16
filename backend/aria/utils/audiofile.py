"""Temp-file handling for uploaded audio.

Two safeguards the previous version lacked: an explicit size ceiling before the
base64 payload is decoded (a malformed upload could otherwise fill the disk of a
shelter laptop) and cleanup that runs even when the pipeline raises.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from aria.config import settings
from aria.core.errors import ValidationError
from aria.core.logging import get_logger

log = get_logger("audio")


def decode_upload(audio_b64: str) -> bytes:
    """Decode a base64 upload, enforcing the configured size limit."""
    payload = (audio_b64 or "").strip()
    if not payload:
        raise ValidationError("No audio payload was supplied")

    # Base64 inflates by 4/3; check before allocating the decoded buffer.
    estimated = len(payload) * 3 // 4
    limit = settings.api.max_upload_bytes
    if estimated > limit:
        raise ValidationError(
            f"Audio is too large ({estimated // 1024} KB); the limit is {limit // 1024} KB",
            limit_bytes=limit,
        )

    if "," in payload[:64] and payload[:5].lower() == "data:":
        payload = payload.split(",", 1)[1]  # tolerate data: URLs from the renderer

    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(f"Audio payload is not valid base64: {exc}") from exc


def write_temp_audio(data: bytes, request_id: str, suffix: str = "raw") -> Path:
    path = Path(settings.paths.temp) / f"{request_id}_{suffix}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def clean_path(request_id: str) -> Path:
    return Path(settings.paths.temp) / f"{request_id}_clean.wav"


def cleanup(request_id: str) -> None:
    if settings.audio.keep_temp_audio:
        return
    for suffix in ("_raw.wav", "_clean.wav"):
        path = Path(settings.paths.temp) / f"{request_id}{suffix}"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            log.warning("Could not remove %s: %s", path, exc)


@contextmanager
def temp_audio(data: bytes, request_id: str) -> Iterator[tuple[Path, Path]]:
    """Yield ``(raw_path, clean_path)`` and always clean up afterwards."""
    raw = write_temp_audio(data, request_id)
    try:
        yield raw, clean_path(request_id)
    finally:
        cleanup(request_id)
