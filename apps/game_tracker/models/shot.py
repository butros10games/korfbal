"""Module contains the Shot model for the game_tracker app."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string


class Shot(models.Model):
    """Model for a shot in a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="shots",
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="shots",
    )
    match_part: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    for_team: models.BooleanField[bool, bool] = models.BooleanField(default=True)
    scored: models.BooleanField[bool, bool] = models.BooleanField(default=False)
    shot_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GoalType",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    time: models.DateTimeField[datetime, datetime | None] = models.DateTimeField(
        default=None,
        blank=True,
        null=True,
    )

    class Meta:
        """Meta options for Shot."""

        indexes = (
            # Speeds up score/stat aggregations for a match.
            models.Index(fields=["match_data", "team", "scored"]),
            # Speeds up per-player season stats.
            models.Index(fields=["player", "scored"]),
            # Speeds up per-match shot timelines.
            models.Index(fields=["match_data", "time"]),
            # Speeds up scored-event timelines.
            models.Index(fields=["match_data", "scored", "time"]),
        )

    def __str__(self) -> str:
        """Return the string representation of the shot.

        Returns:
            str: A string representation of the shot.

        """
        return f"Shot {self.id_uuid} - {self.player} - {self.team} - {self.match_part} - {self.scored}"  # noqa: E501
