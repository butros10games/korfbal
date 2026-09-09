"""Visible audit event querysets and aggregate reporting payloads."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from django.db.models import Count, Max, Min, Q, QuerySet
from django.db.models.functions import TruncHour

from apps.audit.domain.producer_health import producer_health_item
from apps.audit.models import AuditEvent


def visible_audit_events(
    *, actor_id: str | None, is_staff: bool
) -> QuerySet[AuditEvent]:
    """Apply the shared staff, actor, club, and severity visibility rules."""
    events = AuditEvent.objects.all()
    if is_staff:
        return events
    if actor_id is None:
        return events.none()
    return events.filter(
        Q(actor_id=actor_id)
        | Q(club_id__exact="")
        | Q(severity__in=["info", "warning", "error"])
    )


def audit_summary(
    *, events: QuerySet[AuditEvent], now: datetime, window_hours: int
) -> dict[str, Any]:
    """Build the summary report from visible events."""
    cutoff = now - timedelta(hours=window_hours)
    queryset = events.filter(occurred_at__gte=cutoff)
    bounds = queryset.aggregate(
        total=Count("id_uuid"), latest=Max("occurred_at"), oldest=Min("occurred_at")
    )
    by_severity = {
        row["severity"]: row["count"]
        for row in queryset.values("severity").annotate(count=Count("id_uuid"))
    }
    by_source = [
        {"source_system": row["source_system"], "count": row["count"]}
        for row in queryset
        .values("source_system")
        .annotate(count=Count("id_uuid"))
        .order_by("-count", "source_system")[:20]
    ]
    top_events = [
        {"event_name": row["event_name"], "count": row["count"]}
        for row in queryset
        .values("event_name")
        .annotate(count=Count("id_uuid"))
        .order_by("-count", "event_name")[:20]
    ]
    return {
        "window_hours": window_hours,
        "cutoff": cutoff.isoformat(),
        "total": bounds["total"],
        "by_severity": by_severity,
        "by_source": by_source,
        "top_events": top_events,
        "latest": bounds["latest"].isoformat() if bounds["latest"] else None,
        "oldest": bounds["oldest"].isoformat() if bounds["oldest"] else None,
    }


def audit_producers(
    *, events: QuerySet[AuditEvent], now: datetime, window_hours: int
) -> dict[str, Any]:
    """Build the producers report from visible events."""
    cutoff = now - timedelta(hours=window_hours)
    queryset = events.filter(occurred_at__gte=cutoff)
    producers = list(
        queryset
        .values("source_system")
        .annotate(
            total=Count("id_uuid"),
            errors=Count("id_uuid", filter=Q(severity="error")),
            warnings=Count("id_uuid", filter=Q(severity="warning")),
            last_seen=Max("occurred_at"),
        )
        .order_by("-total", "source_system")
    )
    return {
        "window_hours": window_hours,
        "cutoff": cutoff.isoformat(),
        "count": len(producers),
        "items": [
            {
                "source_system": row["source_system"],
                "total": row["total"],
                "errors": row["errors"],
                "warnings": row["warnings"],
                "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            }
            for row in producers
        ],
    }


def audit_trends(
    *, events: QuerySet[AuditEvent], now: datetime, window_hours: int
) -> dict[str, Any]:
    """Build the trends report from visible events."""
    cutoff = now - timedelta(hours=window_hours)
    previous_cutoff = cutoff - timedelta(hours=window_hours)
    current_queryset = events.filter(occurred_at__gte=cutoff)
    previous_queryset = events.filter(
        occurred_at__gte=previous_cutoff, occurred_at__lt=cutoff
    )
    trend_rows = list(
        current_queryset
        .annotate(hour=TruncHour("occurred_at"))
        .values("hour")
        .annotate(
            total=Count("id_uuid"),
            debug=Count("id_uuid", filter=Q(severity="debug")),
            info=Count("id_uuid", filter=Q(severity="info")),
            warnings=Count("id_uuid", filter=Q(severity="warning")),
            errors=Count("id_uuid", filter=Q(severity="error")),
        )
        .order_by("hour")
    )
    trend_by_hour = {row["hour"]: row for row in trend_rows if row["hour"] is not None}
    start_hour = cutoff.replace(minute=0, second=0, microsecond=0)
    end_hour = now.replace(minute=0, second=0, microsecond=0)
    points: list[dict[str, object]] = []
    cursor = start_hour
    while cursor <= end_hour:
        row = trend_by_hour.get(cursor)
        points.append({
            "hour": cursor.isoformat(),
            "total": row["total"] if row else 0,
            "by_severity": {
                "debug": row["debug"] if row else 0,
                "info": row["info"] if row else 0,
                "warning": row["warnings"] if row else 0,
                "error": row["errors"] if row else 0,
            },
        })
        cursor += timedelta(hours=1)
    current_metrics = current_queryset.aggregate(
        total=Count("id_uuid"), errors=Count("id_uuid", filter=Q(severity="error"))
    )
    previous_metrics = previous_queryset.aggregate(
        total=Count("id_uuid"), errors=Count("id_uuid", filter=Q(severity="error"))
    )
    current_total = int(current_metrics["total"] or 0)
    current_errors = int(current_metrics["errors"] or 0)
    previous_total = int(previous_metrics["total"] or 0)
    previous_errors = int(previous_metrics["errors"] or 0)
    current_error_rate = current_errors / current_total * 100 if current_total else 0.0
    previous_error_rate = (
        previous_errors / previous_total * 100 if previous_total else 0.0
    )
    return {
        "window_hours": window_hours,
        "cutoff": cutoff.isoformat(),
        "points": points,
        "error_rate": {
            "current": round(current_error_rate, 3),
            "previous": round(previous_error_rate, 3),
            "delta": round(current_error_rate - previous_error_rate, 3),
        },
    }


def audit_producer_health(
    *, events: QuerySet[AuditEvent], now: datetime, window_hours: int
) -> dict[str, Any]:
    """Build the producer health report from visible events."""
    cutoff = now - timedelta(hours=window_hours)
    previous_cutoff = cutoff - timedelta(hours=window_hours)
    current_rows, previous_rows = _aggregated_rows(
        events=events, cutoff=cutoff, previous_cutoff=previous_cutoff
    )
    previous_by_source = {
        row["source_system"]: row for row in previous_rows if row["source_system"]
    }
    items = [
        producer_health_item(
            row=row,
            previous_row=previous_by_source.get(str(row["source_system"])),
            now=now,
            window_hours=window_hours,
        )
        for row in current_rows
    ]
    items.sort(
        key=lambda item: (cast(float, item["score"]), str(item["source_system"])),
        reverse=True,
    )
    return {
        "window_hours": window_hours,
        "cutoff": cutoff.isoformat(),
        "count": len(items),
        "items": items,
    }


def _aggregated_rows(
    *, events: QuerySet[AuditEvent], cutoff: datetime, previous_cutoff: datetime
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    current_queryset = events.filter(occurred_at__gte=cutoff)
    previous_queryset = events.filter(
        occurred_at__gte=previous_cutoff, occurred_at__lt=cutoff
    )
    current_rows = list(
        current_queryset
        .values("source_system")
        .annotate(
            total=Count("id_uuid"),
            errors=Count("id_uuid", filter=Q(severity="error")),
            warnings=Count("id_uuid", filter=Q(severity="warning")),
            last_seen=Max("occurred_at"),
        )
        .order_by("source_system")
    )
    previous_rows = list(
        previous_queryset
        .values("source_system")
        .annotate(
            total=Count("id_uuid"), errors=Count("id_uuid", filter=Q(severity="error"))
        )
        .order_by("source_system")
    )
    return (current_rows, previous_rows)
