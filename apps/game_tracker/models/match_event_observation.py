"""Immutable reports made by clients about canonical match events."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models

from .constants import team_model_string


class MatchEventObservation(models.Model):
    """One device/team report attached to a canonical event version."""

    ORIGIN_CANONICAL = "canonical"
    ORIGIN_MATCHED = "matched"
    ORIGIN_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (ORIGIN_CANONICAL, "Created canonical event"),
        (ORIGIN_MATCHED, "Matched existing event"),
    ]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="event_observations",
    )
    event: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchEvent",
        on_delete=models.CASCADE,
        related_name="observations",
    )
    event_id: str
    match_data_id: str
    command_id: models.UUIDField[str, str | None] = models.UUIDField(
        null=True,
        blank=True,
    )
    reporting_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.SET_NULL,
        related_name="match_event_observations",
        null=True,
        blank=True,
    )
    reporting_team_id: str | None
    actor_id: str | None
    actor: models.ForeignKey[Any, Any] = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="match_event_observations",
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
    client_sequence: models.PositiveBigIntegerField[int, int | None] = (
        models.PositiveBigIntegerField(null=True, blank=True)
    )
    effective_at: models.DateTimeField[datetime, datetime | None] = (
        models.DateTimeField(null=True, blank=True)
    )
    elapsed_ms: models.PositiveBigIntegerField[int, int | None] = (
        models.PositiveBigIntegerField(null=True, blank=True)
    )
    origin: models.CharField[str, str] = models.CharField(
        max_length=12,
        choices=ORIGIN_CHOICES,
        default=ORIGIN_CANONICAL,
    )
    payload: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField(
        default=dict
    )
    recorded_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep command reports unique per event while allowing system facts."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["event", "command_id"],
                condition=models.Q(command_id__isnull=False),
                name="game_tracker_unique_event_command_observation",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["match_data", "recorded_at"],
                name="event_obs_match_time_idx",
            ),
            models.Index(
                fields=["match_data", "reporting_team", "effective_at"],
                name="event_obs_team_time_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return the observation and canonical event identifiers."""
        return f"{self.id_uuid} -> {self.event_id}"
