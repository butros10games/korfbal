"""Serializers for audit event ingestion and timeline output."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.audit.models import AuditEvent


class AuditEventIngestSerializer(serializers.Serializer):
    """Validate incoming normalized audit events."""

    event_name = serializers.CharField(max_length=128)
    source_system = serializers.CharField(max_length=64, required=False)
    occurred_at = serializers.DateTimeField(required=False)
    severity = serializers.ChoiceField(
        choices=["debug", "info", "warning", "error"],
        required=False,
        default="info",
    )

    actor_id = serializers.CharField(max_length=128, required=False, allow_blank=True)
    actor_type = serializers.CharField(max_length=64, required=False, allow_blank=True)
    session_id = serializers.CharField(
        max_length=128,
        required=False,
        allow_blank=True,
    )
    trace_id = serializers.CharField(max_length=128, required=False, allow_blank=True)
    subject_type = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
    )
    subject_id = serializers.CharField(
        max_length=128,
        required=False,
        allow_blank=True,
    )
    club_id = serializers.CharField(max_length=64, required=False, allow_blank=True)

    message = serializers.CharField(required=False, allow_blank=True)
    metadata = serializers.DictField(required=False, default=dict)
    payload = serializers.DictField(required=False, default=dict)


class AuditEventBulkIngestSerializer(serializers.Serializer):
    """Validate bulk ingest payload with a list of events."""

    events = AuditEventIngestSerializer(many=True)


class AuditEventTimelineSerializer(serializers.ModelSerializer[AuditEvent]):
    """Serialize audit rows for timeline endpoints."""

    class Meta:
        """Serializer metadata."""

        model = AuditEvent
        fields = (
            "id_uuid",
            "occurred_at",
            "created_at",
            "source_system",
            "event_name",
            "severity",
            "actor_id",
            "actor_type",
            "session_id",
            "trace_id",
            "subject_type",
            "subject_id",
            "club_id",
            "message",
            "metadata",
            "payload",
            "ingested_via",
        )


def compact_timeline_payload(
    items: list[dict[str, Any]],
    *,
    next_cursor: str | None = None,
    has_more: bool = False,
) -> dict[str, object]:
    """Shape timeline response payload consistently."""
    return {
        "count": len(items),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "items": items,
    }
