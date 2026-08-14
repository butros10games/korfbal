"""Prometheus metrics for the match SSE transport."""

from prometheus_client import Counter, Gauge


SSE_ACTIVE_CONNECTIONS = Gauge(
    "korfbal_sse_active_connections",
    "Currently open match SSE connections.",
    multiprocess_mode="livesum",
)
SSE_EVENTS_SENT = Counter(
    "korfbal_sse_events_sent_total",
    "SSE frames sent to match clients.",
    labelnames=("event",),
)
SSE_REJECTIONS = Counter(
    "korfbal_sse_rejections_total",
    "Rejected match SSE connection attempts.",
    labelnames=("reason",),
)
SSE_PUBLICATIONS = Counter(
    "korfbal_sse_publications_total",
    "Committed match invalidations published to the channel layer.",
    labelnames=("result",),
)
