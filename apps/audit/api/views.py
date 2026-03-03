"""Views for unified audit ingestion and timeline retrieval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID

from bg_audit_events import UnifiedAuditEvent
from django.conf import settings
from django.db.models import Count, Max, Q, QuerySet
from django.db.models.functions import TruncHour
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent

from .serializers import (
    AuditEventBulkIngestSerializer,
    AuditEventIngestSerializer,
    AuditEventTimelineSerializer,
    compact_timeline_payload,
)


HTTP_STATUS_CREATED = status.HTTP_201_CREATED
HTTP_STATUS_FORBIDDEN = status.HTTP_403_FORBIDDEN
HTTP_STATUS_OK = status.HTTP_200_OK
HTTP_STATUS_BAD_REQUEST = status.HTTP_400_BAD_REQUEST

DEFAULT_TIMELINE_LIMIT = 100
MAX_TIMELINE_LIMIT = 250
DEFAULT_SUMMARY_WINDOW_HOURS = 24
MAX_SUMMARY_WINDOW_HOURS = 168


def _request_is_staff(request: Request) -> bool:
    return bool(getattr(request.user, "is_staff", False))


def _coerce_count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _runtime_ingest_token() -> str:
    return str(getattr(settings, "KORFBAL_AUDIT_INGEST_TOKEN", "") or "").strip()


def _token_valid(request: Request) -> bool:
    expected_token = _runtime_ingest_token()
    if not expected_token:
        return True
    incoming_token = str(request.headers.get("X-Audit-Token", "")).strip()
    return incoming_token == expected_token


def _normalize_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _cursor_value(*, row: AuditEvent) -> str:
    return f"{row.occurred_at.isoformat()}::{row.id_uuid}"


def _parse_cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if not value:
        return None

    if "::" not in value:
        return None

    raw_ts, raw_id = value.split("::", maxsplit=1)
    normalized = _normalize_datetime(raw_ts)
    if normalized is None:
        return None

    try:
        cursor_id = UUID(raw_id)
    except ValueError:
        return None

    return normalized, cursor_id


def _create_row(*, request: Request, event: UnifiedAuditEvent) -> AuditEvent:
    if request.user.is_authenticated and not event.actor_id:
        event.actor_id = str(request.user.pk)
        if not event.actor_type:
            event.actor_type = "django_user"

    return AuditEvent(
        occurred_at=event.occurred_at,
        source_system=event.source_system,
        event_name=event.event_name,
        severity=event.severity,
        actor_id=event.actor_id or "",
        actor_type=event.actor_type or "",
        session_id=event.session_id or "",
        trace_id=event.trace_id or "",
        subject_type=event.subject_type or "",
        subject_id=event.subject_id or "",
        club_id=event.club_id or "",
        message=event.message,
        metadata=event.metadata,
        payload=event.payload,
        ingested_via="api",
    )


class AuditEventIngestAPIView(APIView):
    """Receive normalized audit events from any producer/runtime."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Ingest a normalized audit event."""
        if not _token_valid(request):
            return Response(
                {"detail": "Invalid audit ingest token."},
                status=HTTP_STATUS_FORBIDDEN,
            )

        serializer = AuditEventIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = dict(serializer.validated_data)

        event = UnifiedAuditEvent.from_mapping(data, default_source="unknown")
        row = _create_row(request=request, event=event)
        row.save()

        return Response(
            {
                "id_uuid": str(row.id_uuid),
                "occurred_at": row.occurred_at.isoformat(),
            },
            status=HTTP_STATUS_CREATED,
        )


class AuditEventBulkIngestAPIView(APIView):
    """Receive multiple normalized audit events in a single request."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Ingest a batch of normalized audit events."""
        if not _token_valid(request):
            return Response(
                {"detail": "Invalid audit ingest token."},
                status=HTTP_STATUS_FORBIDDEN,
            )

        serializer = AuditEventBulkIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        validated_events = serializer.validated_data["events"]
        rows: list[AuditEvent] = []
        for event_payload in validated_events:
            event = UnifiedAuditEvent.from_mapping(
                dict(event_payload),
                default_source="unknown",
            )
            rows.append(_create_row(request=request, event=event))

        created_rows = AuditEvent.objects.bulk_create(rows)
        created_ids = [str(row.id_uuid) for row in created_rows]

        return Response(
            {
                "created": len(created_rows),
                "ids": created_ids,
            },
            status=HTTP_STATUS_CREATED,
        )


class AuditTimelineAPIView(APIView):
    """List audit events as a searchable timeline."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return timeline results with filter support."""
        queryset = self._build_queryset(request)

        limit_raw = request.query_params.get("limit")
        limit = DEFAULT_TIMELINE_LIMIT
        if limit_raw:
            try:
                limit = int(limit_raw)
            except ValueError:
                limit = DEFAULT_TIMELINE_LIMIT
        limit = max(1, min(limit, MAX_TIMELINE_LIMIT))

        cursor = _parse_cursor(request.query_params.get("cursor"))
        if request.query_params.get("cursor") and cursor is None:
            return Response(
                {"detail": "Invalid cursor format."},
                status=HTTP_STATUS_BAD_REQUEST,
            )

        if cursor is not None:
            cursor_dt, cursor_id = cursor
            queryset = queryset.filter(
                Q(occurred_at__lt=cursor_dt)
                | Q(occurred_at=cursor_dt, id_uuid__lt=cursor_id)
            )

        rows = list(queryset[: limit + 1])
        has_more = len(rows) > limit
        page_rows = rows[:limit]

        next_cursor = None
        if has_more and page_rows:
            next_cursor = _cursor_value(row=page_rows[-1])

        serialized_rows = AuditEventTimelineSerializer(page_rows, many=True).data
        items: list[dict[str, Any]] = [dict(row) for row in serialized_rows]
        return Response(
            compact_timeline_payload(
                items,
                next_cursor=next_cursor,
                has_more=has_more,
            ),
            status=HTTP_STATUS_OK,
        )

    def _build_queryset(self, request: Request) -> QuerySet[AuditEvent]:
        queryset = AuditEvent.objects.all().order_by("-occurred_at", "-id_uuid")

        source = (request.query_params.get("source") or "").strip()
        if source:
            queryset = queryset.filter(source_system=source)

        event_name = (request.query_params.get("event_name") or "").strip()
        if event_name:
            queryset = queryset.filter(event_name=event_name)

        actor_id = (request.query_params.get("actor_id") or "").strip()
        if actor_id:
            queryset = queryset.filter(actor_id=actor_id)

        club_id = (request.query_params.get("club_id") or "").strip()
        if club_id:
            queryset = queryset.filter(club_id=club_id)

        search_term = (request.query_params.get("search") or "").strip()
        if search_term:
            queryset = queryset.filter(
                Q(event_name__icontains=search_term)
                | Q(message__icontains=search_term)
                | Q(actor_id__icontains=search_term)
                | Q(subject_id__icontains=search_term)
            )

        since = _normalize_datetime(request.query_params.get("since"))
        if since:
            queryset = queryset.filter(occurred_at__gte=since)

        until = _normalize_datetime(request.query_params.get("until"))
        if until:
            queryset = queryset.filter(occurred_at__lte=until)

        if _request_is_staff(request):
            return queryset

        if request.user.is_authenticated:
            return queryset.filter(
                Q(actor_id=str(request.user.pk))
                | Q(club_id__exact="")
                | Q(severity__in=["info", "warning", "error"])
            )

        return queryset.none()


class AuditSummaryAPIView(APIView):
    """Return aggregate audit statistics for dashboards/operations."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return counts by severity/source/event over a recent time window."""
        window_hours = self._window_hours(request)
        cutoff = timezone.now() - timedelta(hours=window_hours)

        queryset = AuditEvent.objects.filter(occurred_at__gte=cutoff)
        if not _request_is_staff(request):
            queryset = queryset.filter(
                Q(actor_id=str(request.user.pk))
                | Q(club_id__exact="")
                | Q(severity__in=["info", "warning", "error"])
            )

        total = queryset.count()

        by_severity = {
            row["severity"]: row["count"]
            for row in queryset.values("severity").annotate(count=Count("id_uuid"))
        }
        by_source = [
            {
                "source_system": row["source_system"],
                "count": row["count"],
            }
            for row in queryset
            .values("source_system")
            .annotate(count=Count("id_uuid"))
            .order_by("-count", "source_system")[:20]
        ]
        top_events = [
            {
                "event_name": row["event_name"],
                "count": row["count"],
            }
            for row in queryset
            .values("event_name")
            .annotate(count=Count("id_uuid"))
            .order_by("-count", "event_name")[:20]
        ]

        latest = queryset.order_by("-occurred_at", "-id_uuid").first()
        oldest = queryset.order_by("occurred_at", "id_uuid").first()

        return Response(
            {
                "window_hours": window_hours,
                "cutoff": cutoff.isoformat(),
                "total": total,
                "by_severity": by_severity,
                "by_source": by_source,
                "top_events": top_events,
                "latest": latest.occurred_at.isoformat() if latest else None,
                "oldest": oldest.occurred_at.isoformat() if oldest else None,
            },
            status=HTTP_STATUS_OK,
        )

    def _window_hours(self, request: Request) -> int:
        raw = request.query_params.get("window_hours")
        if not raw:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        try:
            parsed = int(raw)
        except ValueError:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        return max(1, min(parsed, MAX_SUMMARY_WINDOW_HOURS))


class AuditProducerStatsAPIView(APIView):
    """Return producer/source health statistics over a configurable window."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return per-source totals, warning/error counts, and last seen timestamp."""
        window_hours = self._window_hours(request)
        cutoff = timezone.now() - timedelta(hours=window_hours)

        queryset = AuditEvent.objects.filter(occurred_at__gte=cutoff)
        if not _request_is_staff(request):
            queryset = queryset.filter(
                Q(actor_id=str(request.user.pk))
                | Q(club_id__exact="")
                | Q(severity__in=["info", "warning", "error"])
            )

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

        return Response(
            {
                "window_hours": window_hours,
                "cutoff": cutoff.isoformat(),
                "count": len(producers),
                "items": [
                    {
                        "source_system": row["source_system"],
                        "total": row["total"],
                        "errors": row["errors"],
                        "warnings": row["warnings"],
                        "last_seen": (
                            row["last_seen"].isoformat() if row["last_seen"] else None
                        ),
                    }
                    for row in producers
                ],
            },
            status=HTTP_STATUS_OK,
        )

    def _window_hours(self, request: Request) -> int:
        raw = request.query_params.get("window_hours")
        if not raw:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        try:
            parsed = int(raw)
        except ValueError:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        return max(1, min(parsed, MAX_SUMMARY_WINDOW_HOURS))


class AuditTrendStatsAPIView(APIView):
    """Return hourly trend points and error-rate delta for dashboards/alerting."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return hourly event buckets and compare error-rate against prior window."""
        now = timezone.now()
        window_hours = self._window_hours(request)
        cutoff = now - timedelta(hours=window_hours)
        previous_cutoff = cutoff - timedelta(hours=window_hours)

        current_queryset = AuditEvent.objects.filter(occurred_at__gte=cutoff)
        previous_queryset = AuditEvent.objects.filter(
            occurred_at__gte=previous_cutoff,
            occurred_at__lt=cutoff,
        )

        if not _request_is_staff(request):
            visibility_filter = (
                Q(actor_id=str(request.user.pk))
                | Q(club_id__exact="")
                | Q(severity__in=["info", "warning", "error"])
            )
            current_queryset = current_queryset.filter(visibility_filter)
            previous_queryset = previous_queryset.filter(visibility_filter)

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
        trend_by_hour = {
            row["hour"]: row for row in trend_rows if row["hour"] is not None
        }

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

        current_total = current_queryset.count()
        current_errors = current_queryset.filter(severity="error").count()
        previous_total = previous_queryset.count()
        previous_errors = previous_queryset.filter(severity="error").count()

        current_error_rate = (
            (current_errors / current_total) * 100 if current_total else 0.0
        )
        previous_error_rate = (
            (previous_errors / previous_total) * 100 if previous_total else 0.0
        )

        return Response(
            {
                "window_hours": window_hours,
                "cutoff": cutoff.isoformat(),
                "points": points,
                "error_rate": {
                    "current": round(current_error_rate, 3),
                    "previous": round(previous_error_rate, 3),
                    "delta": round(current_error_rate - previous_error_rate, 3),
                },
            },
            status=HTTP_STATUS_OK,
        )

    def _window_hours(self, request: Request) -> int:
        raw = request.query_params.get("window_hours")
        if not raw:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        try:
            parsed = int(raw)
        except ValueError:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        return max(1, min(parsed, MAX_SUMMARY_WINDOW_HOURS))


class AuditProducerHealthAPIView(APIView):
    """Rank producer health using weighted risk metrics for operations."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return per-producer risk scores sorted from worst to best."""
        now = timezone.now()
        window_hours = self._window_hours(request)
        cutoff = now - timedelta(hours=window_hours)
        previous_cutoff = cutoff - timedelta(hours=window_hours)

        current_rows, previous_rows = self._aggregated_rows(
            request=request,
            cutoff=cutoff,
            previous_cutoff=previous_cutoff,
        )
        previous_by_source = {
            row["source_system"]: row for row in previous_rows if row["source_system"]
        }
        items = [
            self._health_item(
                row=row,
                previous_row=previous_by_source.get(str(row["source_system"])),
                now=now,
                window_hours=window_hours,
            )
            for row in current_rows
        ]

        items.sort(
            key=lambda item: (float(item["score"]), str(item["source_system"])),
            reverse=True,
        )

        return Response(
            {
                "window_hours": window_hours,
                "cutoff": cutoff.isoformat(),
                "count": len(items),
                "items": items,
            },
            status=HTTP_STATUS_OK,
        )

    def _aggregated_rows(
        self,
        *,
        request: Request,
        cutoff: datetime,
        previous_cutoff: datetime,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        current_queryset = AuditEvent.objects.filter(occurred_at__gte=cutoff)
        previous_queryset = AuditEvent.objects.filter(
            occurred_at__gte=previous_cutoff,
            occurred_at__lt=cutoff,
        )

        if not _request_is_staff(request):
            visibility_filter = (
                Q(actor_id=str(request.user.pk))
                | Q(club_id__exact="")
                | Q(severity__in=["info", "warning", "error"])
            )
            current_queryset = current_queryset.filter(visibility_filter)
            previous_queryset = previous_queryset.filter(visibility_filter)

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
                total=Count("id_uuid"),
                errors=Count("id_uuid", filter=Q(severity="error")),
            )
            .order_by("source_system")
        )

        return current_rows, previous_rows

    def _health_item(
        self,
        *,
        row: dict[str, object],
        previous_row: dict[str, object] | None,
        now: datetime,
        window_hours: int,
    ) -> dict[str, object]:
        source = str(row["source_system"])
        total = _coerce_count(row["total"])
        errors = _coerce_count(row["errors"])
        warnings = _coerce_count(row["warnings"])
        last_seen = row["last_seen"]

        previous_total = _coerce_count(previous_row["total"]) if previous_row else 0
        previous_errors = _coerce_count(previous_row["errors"]) if previous_row else 0

        current_error_rate = (errors / total) if total else 0.0
        previous_error_rate = (
            (previous_errors / previous_total) if previous_total else 0.0
        )
        error_rate_delta = current_error_rate - previous_error_rate

        last_seen_hours = self._last_seen_hours(
            last_seen=last_seen,
            now=now,
            window_hours=window_hours,
        )
        staleness_ratio = min(last_seen_hours, 24.0) / 24.0
        recency_factor = 1.0 - staleness_ratio
        warning_rate = (warnings / total) if total else 0.0
        normalized_volume = min(total, 100) / 100

        risk_score = (
            current_error_rate * 60
            + max(0.0, error_rate_delta) * 20
            + normalized_volume * 10
            + recency_factor * 5
            + warning_rate * 5
        ) * 100

        return {
            "source_system": source,
            "score": round(risk_score, 3),
            "factors": {
                "current_error_rate": round(current_error_rate * 100, 3),
                "previous_error_rate": round(previous_error_rate * 100, 3),
                "error_rate_delta": round(error_rate_delta * 100, 3),
                "warning_rate": round(warning_rate * 100, 3),
                "normalized_volume": round(normalized_volume, 3),
                "last_seen_hours": round(last_seen_hours, 3),
            },
            "totals": {
                "current": {
                    "total": total,
                    "errors": errors,
                    "warnings": warnings,
                },
                "previous": {
                    "total": previous_total,
                    "errors": previous_errors,
                },
            },
            "last_seen": (
                last_seen.isoformat() if isinstance(last_seen, datetime) else None
            ),
        }

    def _last_seen_hours(
        self,
        *,
        last_seen: object,
        now: datetime,
        window_hours: int,
    ) -> float:
        if isinstance(last_seen, datetime):
            return max(
                0.0,
                (now - last_seen).total_seconds() / 3600,
            )

        return float(window_hours)

    def _window_hours(self, request: Request) -> int:
        raw = request.query_params.get("window_hours")
        if not raw:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        try:
            parsed = int(raw)
        except ValueError:
            return DEFAULT_SUMMARY_WINDOW_HOURS

        return max(1, min(parsed, MAX_SUMMARY_WINDOW_HOURS))
