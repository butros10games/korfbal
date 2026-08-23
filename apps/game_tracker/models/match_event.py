"""Append-only domain event envelope for match tracking."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models

from .constants import team_model_string


class MatchEvent(models.Model):
    """Versioned, ordered audit envelope around a typed tracker record."""

    STATUS_ACTIVE = "active"
    STATUS_SUPERSEDED = "superseded"
    STATUS_RETRACTED = "retracted"
    STATUS_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_SUPERSEDED, "Superseded"),
        (STATUS_RETRACTED, "Retracted"),
    ]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="domain_events",
    )
    match_data_id: str
    sequence: models.PositiveBigIntegerField[int, int] = (
        models.PositiveBigIntegerField()
    )
    logical_id: models.UUIDField[str, str] = models.UUIDField(
        default=uuidv7,
        editable=False,
    )
    kind: models.CharField[str, str] = models.CharField(max_length=64)
    source_type: models.CharField[str, str] = models.CharField(max_length=32)
    source_id: models.UUIDField[str, str] = models.UUIDField()
    effective_at: models.DateTimeField[datetime, datetime | None] = (
        models.DateTimeField(null=True, blank=True)
    )
    elapsed_ms: models.PositiveBigIntegerField[int, int | None] = (
        models.PositiveBigIntegerField(null=True, blank=True)
    )
    match_part: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchPart",
        on_delete=models.SET_NULL,
        related_name="domain_events",
        null=True,
        blank=True,
    )
    recorded_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    actor: models.ForeignKey[Any, Any] = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="recorded_match_events",
        null=True,
        blank=True,
    )
    source_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.SET_NULL,
        related_name="recorded_match_events",
        null=True,
        blank=True,
    )
    command_id: models.UUIDField[str, str | None] = models.UUIDField(
        null=True,
        blank=True,
    )
    source: models.CharField[str, str] = models.CharField(
        max_length=32,
        default="system",
    )
    device_id: models.CharField[str, str] = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    session_id: models.CharField[str, str] = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    payload_version: models.PositiveSmallIntegerField[int, int] = (
        models.PositiveSmallIntegerField(default=1)
    )
    payload: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField(
        default=dict
    )
    status: models.CharField[str, str] = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )
    supersedes: models.ForeignKey[Any, Any] = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="superseded_by",
        null=True,
        blank=True,
    )

    class Meta:
        """Enforce one total event order per match."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match_data", "sequence"],
                name="game_tracker_unique_event_sequence",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["match_data", "-sequence"],
                name="match_event_match_seq_idx",
            ),
            models.Index(
                fields=["match_data", "source_type", "source_id", "status"],
                name="match_event_source_idx",
            ),
            models.Index(
                fields=["match_data", "effective_at"],
                name="match_event_effective_idx",
            ),
            models.Index(
                fields=["match_data", "logical_id", "-sequence"],
                name="match_event_logical_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return a concise event identifier."""
        return f"{self.match_data_id}@{self.sequence}:{self.kind}"
