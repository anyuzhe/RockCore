"""Timezone-safe conversion helpers for user-facing timestamps."""

from datetime import datetime, timezone, tzinfo
from typing import Any


def as_utc_isoformat(value: datetime | None) -> str:
    """Serialize a database datetime as an unambiguous UTC ISO timestamp.

    SQLite commonly returns timezone-aware SQLAlchemy ``DateTime`` values as
    naive objects. RockCore writes those columns in UTC, so a naive value from
    the database must be labelled as UTC before it crosses the UI boundary.
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def to_local_datetime(value: Any, local_tz: tzinfo | None = None) -> datetime:
    """Parse a stored UTC value and convert it to the user's local timezone."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise TypeError("timestamp must be a datetime or ISO string")

    # All persisted RockCore timestamps are UTC. Treat old SQLite values that
    # have no offset as UTC instead of accidentally treating them as local.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(local_tz) if local_tz else parsed.astimezone()


def format_local_timestamp(
    value: Any,
    fmt: str = "%Y-%m-%d %H:%M",
    *,
    unknown: str = "时间未知",
    include_offset: bool = False,
    local_tz: tzinfo | None = None,
) -> str:
    """Format a UTC timestamp in the user's local timezone."""
    if value in (None, ""):
        return unknown
    try:
        local_value = to_local_datetime(value, local_tz=local_tz)
    except (TypeError, ValueError, OverflowError):
        return str(value)[:16] or unknown

    rendered = local_value.strftime(fmt)
    if not include_offset:
        return rendered
    offset = local_value.utcoffset()
    if offset is None:
        return rendered
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{rendered} UTC{sign}{hours:02d}:{minutes:02d}"
