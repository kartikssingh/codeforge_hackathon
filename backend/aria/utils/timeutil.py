"""Time helpers.

Every timestamp in ARIA is timezone-aware and in the shelter laptop's local
zone.  Naive datetimes were the source of two classes of bug in the previous
build: countdown timers that jumped by the UTC offset, and ``fromisoformat``
crashes when a persisted state file was reloaded on a machine in another zone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def now() -> datetime:
    """Current local time, timezone-aware."""
    return datetime.now().astimezone()


def as_local(value: datetime) -> datetime:
    """Attach the local zone to a naive datetime; convert an aware one."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value.astimezone()


def parse_iso(value: object) -> Optional[datetime]:
    """Parse an ISO-8601 string defensively.  Returns None on anything odd."""
    if isinstance(value, datetime):
        return as_local(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return as_local(datetime.fromisoformat(text))
    except ValueError:
        return None


def minutes_between(start: datetime, end: Optional[datetime] = None) -> float:
    """Whole minutes (fractional) from *start* to *end*, never negative."""
    reference = end or now()
    return max(0.0, (as_local(reference) - as_local(start)).total_seconds() / 60.0)


def hours_between(start: datetime, end: Optional[datetime] = None) -> float:
    return minutes_between(start, end) / 60.0


def plus_minutes(start: datetime, minutes: float) -> datetime:
    return as_local(start) + timedelta(minutes=minutes)


def format_clock(value: Optional[datetime]) -> str:
    """``HH:MM:SS`` for logs and CLI output.  Empty string for None."""
    return as_local(value).strftime("%H:%M:%S") if value else ""
