"""Shared ISO-8601 datetime parsing for planning context providers."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso_datetime(value: object) -> datetime | None:
    """Parse an ISO-8601 string into a UTC-aware datetime, or ``None``.

    Accepts any object; non-string or blank input returns ``None``. Trailing
    ``Z`` is normalized to ``+00:00``, and naive results are assumed UTC. Used
    by the GitHub and Plaky planning context providers so their timestamp
    handling stays consistent.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
