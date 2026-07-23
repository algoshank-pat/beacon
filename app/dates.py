"""Shared date parsing/formatting. All Sheet-facing dates render as
mmddyyyy HH:MM in Central time (America/Chicago), converted from whatever
timezone/format the source produced -- Adzuna, Greenhouse, Lever, and this
app's own CURRENT_TIMESTAMP writes all use different formats.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def parse_datetime(value: str | None) -> datetime | None:
    """Parses a timestamp in any format this app's data sources produce.
    Naive results (no tzinfo) are assumed UTC -- every naive timestamp in
    this codebase comes from either SQLite's CURRENT_TIMESTAMP or
    datetime.now(timezone.utc)."""
    if not value:
        return None
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def format_central(value: str | datetime | None) -> str:
    """Renders as mmddyyyy HH:MM in America/Chicago. Returns '' for
    None/empty input, and the original string unchanged if it can't be
    parsed (never silently drop data the Sheet would otherwise have shown)."""
    if not value:
        return ""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        dt = parse_datetime(str(value))
        if dt is None:
            return str(value)
    return dt.astimezone(CENTRAL).strftime("%m%d%Y %H:%M")
