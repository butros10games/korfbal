"""Database model for normalized audit events."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models


SEVERITY_CHOICES = [
    ("debug", "Debug"),
    ("info", "Info"),
    ("warning", "Warning"),
    ("error", "Error"),
]


class AuditEvent(models.Model):
    """A normalized operational event from any producer/runtime."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    occurred_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        db_index=True,
    )
    source_system: models.CharField[str, str] = models.CharField(
        max_length=64,
        db_index=True,
    )
    event_name: models.CharField[str, str] = models.CharField(
        max_length=128,
        db_index=True,
    )
    severity: models.CharField[str, str] = models.CharField(
        max_length=16,
        choices=SEVERITY_CHOICES,
        default="info",
        db_index=True,
    )

    actor_id: models.CharField[str, str] = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
    )
    actor_type: models.CharField[str, str] = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )
    session_id: models.CharField[str, str] = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
    )
    trace_id: models.CharField[str, str] = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
    )

    subject_type: models.CharField[str, str] = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
    )
    subject_id: models.CharField[str, str] = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
    )
    club_id: models.CharField[str, str] = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
    )

    message: models.TextField[str, str] = models.TextField(blank=True, default="")
    metadata: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField(
        default=dict,
    )
    payload: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField(
        default=dict,
    )

    ingested_via: models.CharField[str, str] = models.CharField(
        max_length=32,
        default="api",
        db_index=True,
    )
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        """Model metadata and frequently-used indexes."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["-occurred_at", "source_system"],
                name="audit_timeline_idx",
            ),
            models.Index(
                fields=["event_name", "actor_id", "club_id"],
                name="audit_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return compact text representation for admin/debug output."""
        return f"{self.source_system}:{self.event_name}:{self.id_uuid}"
