"""Views for unified audit ingestion and timeline retrieval."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from bg_audit_events import UnifiedAuditEvent
from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response

from apps.audit.models import AuditEvent
from apps.audit.queries.reports import (
    audit_producer_health,
    audit_producers,
    audit_summary,
    audit_trends,
    visible_audit_events,
)
from apps.kwt_common.api.base import KorfbalAPIView

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


def _window_hours(request: Request) -> int:
    """Parse the shared reporting window and constrain it to one week."""
    raw = request.query_params.get("window_hours")
    if not raw:
        return DEFAULT_SUMMARY_WINDOW_HOURS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_SUMMARY_WINDOW_HOURS
    return max(1, min(parsed, MAX_SUMMARY_WINDOW_HOURS))


def _visible_events(request: Request) -> QuerySet[AuditEvent]:
    return visible_audit_events(
        actor_id=str(request.user.pk) if request.user.is_authenticated else None,
        is_staff=_request_is_staff(request),
    )


def _runtime_ingest_token() -> str:
    return str(getattr(settings, "KORFBAL_AUDIT_INGEST_TOKEN", "") or "").strip()


def _token_valid(request: Request) -> bool:
    expected_token = _runtime_ingest_token()
    if not expected_token:
        return False
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


class AuditEventIngestAPIView(KorfbalAPIView):
    """Receive normalized audit events from any producer/runtime."""

    permission_classes = (permissions.AllowAny,)

    def post(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
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


class AuditEventBulkIngestAPIView(KorfbalAPIView):
    """Receive multiple normalized audit events in a single request."""

    permission_classes = (permissions.AllowAny,)

    def post(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
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


class AuditTimelineAPIView(KorfbalAPIView):
    """List audit events as a searchable timeline."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
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
        queryset = _visible_events(request).order_by("-occurred_at", "-id_uuid")

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

        return queryset


class AuditSummaryAPIView(KorfbalAPIView):
    """Return aggregate audit statistics for dashboards/operations."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return counts by severity/source/event over a recent time window."""
        return Response(
            audit_summary(
                events=_visible_events(request),
                now=timezone.now(),
                window_hours=_window_hours(request),
            ),
            status=HTTP_STATUS_OK,
        )


class AuditProducerStatsAPIView(KorfbalAPIView):
    """Return producer/source health statistics over a configurable window."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return per-source totals, warning/error counts, and last seen timestamp."""
        return Response(
            audit_producers(
                events=_visible_events(request),
                now=timezone.now(),
                window_hours=_window_hours(request),
            ),
            status=HTTP_STATUS_OK,
        )


class AuditTrendStatsAPIView(KorfbalAPIView):
    """Return hourly trend points and error-rate delta for dashboards/alerting."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return hourly event buckets and compare error-rate against prior window."""
        return Response(
            audit_trends(
                events=_visible_events(request),
                now=timezone.now(),
                window_hours=_window_hours(request),
            ),
            status=HTTP_STATUS_OK,
        )


class AuditProducerHealthAPIView(KorfbalAPIView):
    """Rank producer health using weighted risk metrics for operations."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return per-producer risk scores sorted from worst to best."""
        return Response(
            audit_producer_health(
                events=_visible_events(request),
                now=timezone.now(),
                window_hours=_window_hours(request),
            ),
            status=HTTP_STATUS_OK,
        )
