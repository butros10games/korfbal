"""Prometheus metrics helpers for Korfbal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


try:
    from prometheus_client import Counter, Histogram

    _PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    Counter = Histogram = None  # type: ignore[assignment]
    _PROMETHEUS_AVAILABLE = False


_MAX_LABEL_LENGTH: Final[int] = 200


def _safe_label(value: object, default: str = "unknown") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if len(text) > _MAX_LABEL_LENGTH:
        return text[:_MAX_LABEL_LENGTH]
    return text


if _PROMETHEUS_AVAILABLE:
    REQUEST_DURATION_MS = Histogram(
        "korfbal_request_duration_ms",
        "Request duration in milliseconds",
        ["method", "view", "status"],
        buckets=[
            5,
            10,
            25,
            50,
            100,
            200,
            300,
            500,
            800,
            1000,
            1500,
            2000,
            3000,
            5000,
            10000,
        ],
    )
    SLOW_REQUESTS_TOTAL = Counter(
        "korfbal_slow_requests_total",
        "Requests exceeding slow threshold",
        ["method", "view", "status"],
    )
    SLOW_DB_REQUESTS_TOTAL = Counter(
        "korfbal_slow_db_requests_total",
        "Requests with at least one slow DB query",
        ["method", "view", "status"],
    )
    SLOW_DB_REQUEST_QUERY_COUNT = Histogram(
        "korfbal_slow_db_request_query_count",
        "Count of slow DB queries per request",
        ["method", "view", "status"],
        buckets=[1, 2, 3, 5, 8, 13, 21, 34],
    )
    SLOW_DB_REQUEST_TOTAL_MS = Histogram(
        "korfbal_slow_db_request_total_ms",
        "Total slow DB time per request in milliseconds",
        ["method", "view", "status"],
        buckets=[
            50,
            100,
            200,
            300,
            500,
            800,
            1000,
            1500,
            2000,
            3000,
            5000,
            10000,
        ],
    )
    SLOW_DB_QUERIES_TOTAL = Counter(
        "korfbal_slow_db_queries_total",
        "Slow DB queries detected",
        ["alias"],
    )
    SLOW_DB_QUERY_DURATION_MS = Histogram(
        "korfbal_slow_db_query_duration_ms",
        "Duration of slow DB queries in milliseconds",
        ["alias"],
        buckets=[
            10,
            25,
            50,
            100,
            200,
            300,
            500,
            800,
            1000,
            1500,
            2000,
            3000,
            5000,
            10000,
        ],
    )


@dataclass(frozen=True)
class RequestMetrics:
    """Snapshot of request metrics for Prometheus."""

    method: str | None
    view_name: str | None
    status_code: int | None
    elapsed_ms: int
    slow_db_count: int | None = None
    slow_db_total_ms: int | None = None
    is_slow_request: bool = False


def record_request_metrics(metrics: RequestMetrics) -> None:
    """Record request metrics when Prometheus is available."""
    if not _PROMETHEUS_AVAILABLE:
        return

    labels = {
        "method": _safe_label(metrics.method),
        "view": _safe_label(metrics.view_name),
        "status": _safe_label(metrics.status_code),
    }

    REQUEST_DURATION_MS.labels(**labels).observe(max(0, metrics.elapsed_ms))
    if metrics.is_slow_request:
        SLOW_REQUESTS_TOTAL.labels(**labels).inc()

    if metrics.slow_db_count is not None and metrics.slow_db_count > 0:
        SLOW_DB_REQUESTS_TOTAL.labels(**labels).inc()
        SLOW_DB_REQUEST_QUERY_COUNT.labels(**labels).observe(metrics.slow_db_count)
        if metrics.slow_db_total_ms is not None:
            SLOW_DB_REQUEST_TOTAL_MS.labels(**labels).observe(
                max(0, metrics.slow_db_total_ms)
            )


def record_slow_db_query(*, alias: str, elapsed_ms: int) -> None:
    """Record slow DB query metrics when Prometheus is available."""
    if not _PROMETHEUS_AVAILABLE:
        return

    SLOW_DB_QUERIES_TOTAL.labels(alias=_safe_label(alias)).inc()
    SLOW_DB_QUERY_DURATION_MS.labels(alias=_safe_label(alias)).observe(
        max(0, elapsed_ms)
    )
