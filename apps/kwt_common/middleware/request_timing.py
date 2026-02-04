"""Slow request surfacing middleware.

This middleware complements `SlowQueryLoggingMiddleware`.

Goals:
- Make slow requests visible *without* digging through logs.
- Add lightweight timing headers (works well with browser DevTools).
- Optionally keep a small rolling buffer of slow requests (in cache) that can be
  viewed via a staff-only API endpoint.

Opt-in settings:
- KORFBAL_LOG_SLOW_REQUESTS (bool)
- KORFBAL_SLOW_REQUEST_MS (int)
- KORFBAL_SLOW_REQUEST_BUFFER_SIZE (int)

"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from apps.kwt_common.metrics import RequestMetrics, record_request_metrics
from apps.kwt_common.utils.slow_requests import slow_request_buffer_ttl_s


logger = logging.getLogger("apps.kwt_common.slow_requests")

_CACHE_KEY = "korfbal:slow_requests"


def _append_server_timing(existing: str | None, value: str) -> str:
    if not existing:
        return value
    # Multiple Server-Timing entries are comma-separated.
    return f"{existing}, {value}"


class RequestTimingMiddleware:
    """Measure request duration and surface slow requests."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Create the middleware.

        Args:
            get_response: The next middleware/view callable.

        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Time the request and optionally record slow requests."""
        start = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        # Always attach lightweight timing signals.
        response["X-Korfbal-Request-Duration-Ms"] = str(elapsed_ms)
        response["Server-Timing"] = _append_server_timing(
            response.headers.get("Server-Timing"),
            f"app;dur={elapsed_ms}",
        )

        resolver_match = getattr(request, "resolver_match", None)
        view_name = getattr(resolver_match, "view_name", None)

        slow_queries = getattr(request, "_korfbal_slow_queries", None)
        slow_db_total_ms = None
        if slow_queries:
            slow_db_total_ms = sum(int(q.get("ms", 0)) for q in slow_queries)
            response["Server-Timing"] = _append_server_timing(
                response.headers.get("Server-Timing"),
                f"dbslow;dur={slow_db_total_ms}",
            )
            response["X-Korfbal-Slow-Db-Query-Count"] = str(len(slow_queries))

        threshold_ms = int(getattr(settings, "KORFBAL_SLOW_REQUEST_MS", 500))
        is_slow_request = elapsed_ms >= threshold_ms
        record_request_metrics(
            RequestMetrics(
                method=request.method,
                view_name=view_name,
                status_code=getattr(response, "status_code", None),
                elapsed_ms=elapsed_ms,
                slow_db_count=len(slow_queries) if slow_queries else None,
                slow_db_total_ms=slow_db_total_ms if slow_queries else None,
                is_slow_request=is_slow_request,
            )
        )

        if not bool(getattr(settings, "KORFBAL_LOG_SLOW_REQUESTS", False)):
            return response

        if not is_slow_request:
            return response

        response["X-Korfbal-Slow-Request"] = "1"

        buffer_size = int(getattr(settings, "KORFBAL_SLOW_REQUEST_BUFFER_SIZE", 200))
        buffer_size = max(0, buffer_size)
        if buffer_size == 0:
            return response

        user = getattr(request, "user", None)
        user_id = getattr(user, "id", None)

        entry: dict[str, object] = {
            "ts": timezone.now().isoformat(),
            "method": request.method,
            "path": request.path,
            "view": view_name,
            "status": getattr(response, "status_code", None),
            "duration_ms": elapsed_ms,
            "user_id": user_id,
        }

        if slow_queries:
            entry["slow_db_query_count"] = len(slow_queries)
            entry["slow_db_max_ms"] = max(int(q.get("ms", 0)) for q in slow_queries)
            entry["slow_db_total_ms"] = slow_db_total_ms

            include_sql = bool(getattr(settings, "KORFBAL_SLOW_DB_INCLUDE_SQL", False))
            if include_sql:
                entry["slow_db_queries"] = slow_queries

        # Best-effort rolling buffer in cache.
        try:
            current = cache.get(_CACHE_KEY) or []
            if not isinstance(current, list):
                current = []
            current.insert(0, entry)
            del current[buffer_size:]
            cache.set(_CACHE_KEY, current, timeout=slow_request_buffer_ttl_s())
        except Exception:
            logger.exception("Failed to persist slow request buffer")

        # Still log one line (useful in prod), but the buffer/headers are the UX.
        logger.warning(
            "Slow request %sms (>= %sms) %s %s status=%s user=%s",
            elapsed_ms,
            threshold_ms,
            request.method,
            request.path,
            getattr(response, "status_code", None),
            user_id,
        )

        return response
