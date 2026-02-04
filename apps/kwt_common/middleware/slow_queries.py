"""Slow SQL query logging middleware.

Goal:
- Provide actionable signals for optimizing slow API endpoints.
- Avoid collecting ALL queries in memory (unlike connection.queries in DEBUG).

This middleware is opt-in via settings:
- KORFBAL_LOG_SLOW_DB_QUERIES (bool)
- KORFBAL_SLOW_DB_QUERY_MS (int)

It uses Django's connection execute wrapper, so it can work outside DEBUG.

"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import logging
import operator
import time

from django.conf import settings
from django.db import connections
from django.http import HttpRequest, HttpResponse

from apps.kwt_common.metrics import record_slow_db_query


logger = logging.getLogger("apps.kwt_common.slow_queries")


class SlowQueryLoggingMiddleware:
    """Log slow SQL statements for the duration of a request."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Create the middleware.

        Parameters
        ----------
        get_response:
            The next middleware / view callable.

        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Wrap DB execution during a request and log the slowest queries."""
        if not bool(getattr(settings, "KORFBAL_LOG_SLOW_DB_QUERIES", False)):
            return self.get_response(request)

        threshold_ms = int(getattr(settings, "KORFBAL_SLOW_DB_QUERY_MS", 200))
        threshold_s = max(0.0, threshold_ms / 1000.0)

        # Keep a small top list (slowest queries) so logs stay readable.
        slowest: list[tuple[float, str, str, object]] = []

        def _execute_wrapper_for_alias(alias: str) -> Callable[..., object]:
            def _execute_wrapper(
                execute: Callable[[str, object, bool, dict[str, object]], object],
                sql: str,
                params: object,
                many: bool,
                context: dict[str, object],
            ) -> object:
                start = time.perf_counter()
                try:
                    return execute(sql, params, many, context)
                finally:
                    elapsed = time.perf_counter() - start
                    if elapsed >= threshold_s:
                        elapsed_ms = int(elapsed * 1000)
                        record_slow_db_query(alias=alias, elapsed_ms=elapsed_ms)
                        slowest.append((elapsed, alias, sql, params))
                        slowest.sort(key=operator.itemgetter(0), reverse=True)
                        del slowest[10:]

            return _execute_wrapper

        # Wrap all configured DB connections.
        with ExitStack() as stack:
            for alias in connections:
                stack.enter_context(
                    connections[alias].execute_wrapper(
                        _execute_wrapper_for_alias(alias)
                    )
                )
            response = self.get_response(request)

        include_sql = bool(getattr(settings, "KORFBAL_SLOW_DB_INCLUDE_SQL", False))
        if slowest:
            request._korfbal_slow_queries = [  # type: ignore[attr-defined]
                {
                    "ms": int(elapsed * 1000),
                    "alias": alias,
                    **({"sql": sql, "params": params} if include_sql else {}),
                }
                for elapsed, alias, sql, params in slowest
            ]
        else:
            request._korfbal_slow_queries = []  # type: ignore[attr-defined]

        if slowest:
            user_id = getattr(getattr(request, "user", None), "id", None)
            logger.warning(
                "Slow SQL detected (%s slow queries >= %sms) path=%s user=%s",
                len(slowest),
                threshold_ms,
                request.path,
                user_id,
            )
            for elapsed, alias, sql, params in slowest:
                logger.warning(
                    "  %dms [%s] %s | params=%r",
                    int(elapsed * 1000),
                    alias,
                    sql,
                    params,
                )

        return response
