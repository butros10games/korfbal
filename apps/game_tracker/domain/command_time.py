"""Timestamp rules for tracker commands."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


CLIENT_TIME_MAX_SKEW_SECONDS = 5 * 60


def parse_client_time_iso(value: str) -> datetime | None:
    """Parse an ISO timestamp as UTC, accepting a ``Z`` suffix."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def command_time_from_payload(
    payload: dict[str, Any],
    *,
    server_now: datetime,
) -> datetime:
    """Use a credible client timestamp, otherwise the authoritative clock."""
    server_now = server_now.astimezone(UTC)
    client_time: datetime | None = None

    client_time_ms = payload.get("client_time_ms")
    if isinstance(client_time_ms, int):
        try:
            client_time = datetime.fromtimestamp(client_time_ms / 1000, tz=UTC)
        except (OSError, OverflowError, ValueError):
            client_time = None

    client_time_iso = payload.get("client_time_iso")
    if client_time is None and isinstance(client_time_iso, str):
        client_time = parse_client_time_iso(client_time_iso)

    if client_time is None:
        return server_now
    if abs((client_time - server_now).total_seconds()) > CLIENT_TIME_MAX_SKEW_SECONDS:
        return server_now
    return client_time
