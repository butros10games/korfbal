"""Helpers for surfacing slow requests."""

from __future__ import annotations

from django.conf import settings


def slow_request_buffer_ttl_s() -> int:
    """Return cache TTL (seconds) for the slow request buffer.

    We default to 24h so admins have time to inspect without tailing logs.

    Returns:
        TTL in seconds.

    """
    ttl = int(getattr(settings, "KORFBAL_SLOW_REQUEST_BUFFER_TTL_S", 60 * 60 * 24))
    # Cache backends interpret 0 differently; keep it safe.
    return max(60, ttl)
