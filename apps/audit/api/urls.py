"""URL routes for audit APIs."""

from django.urls import path

from .views import (
    AuditEventBulkIngestAPIView,
    AuditEventIngestAPIView,
    AuditProducerHealthAPIView,
    AuditProducerStatsAPIView,
    AuditSummaryAPIView,
    AuditTimelineAPIView,
    AuditTrendStatsAPIView,
)


urlpatterns = [
    path(
        "events/ingest/",
        AuditEventIngestAPIView.as_view(),
        name="audit-events-ingest",
    ),
    path(
        "events/ingest/bulk/",
        AuditEventBulkIngestAPIView.as_view(),
        name="audit-events-bulk-ingest",
    ),
    path(
        "events/timeline/",
        AuditTimelineAPIView.as_view(),
        name="audit-events-timeline",
    ),
    path(
        "events/summary/",
        AuditSummaryAPIView.as_view(),
        name="audit-events-summary",
    ),
    path(
        "events/producers/",
        AuditProducerStatsAPIView.as_view(),
        name="audit-events-producers",
    ),
    path(
        "events/trends/",
        AuditTrendStatsAPIView.as_view(),
        name="audit-events-trends",
    ),
    path(
        "events/health/",
        AuditProducerHealthAPIView.as_view(),
        name="audit-events-health",
    ),
]
