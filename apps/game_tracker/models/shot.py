"""Module contains the Shot model for the game_tracker app."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string
from .event_projection import EventProjectionModel


class Shot(EventProjectionModel):
    """Model for a shot in a match."""

    objects: ClassVar[models.Manager[Shot]]

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
    player_id: str
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="shots",
    )
    match_data_id: str
    match_part: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    match_part_id: str | None
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    team_id: str | None
    for_team: models.BooleanField[bool, bool] = models.BooleanField(default=True)
    scored: models.BooleanField[bool, bool] = models.BooleanField(default=False)
    shot_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GoalType",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    shot_type_id: str | None
    time: models.DateTimeField[datetime, datetime | None] = models.DateTimeField(
        default=None,
        blank=True,
        null=True,
    )

    class Meta:
        """Meta options for Shot."""

        indexes = (
            # Speeds up score/stat aggregations for a match.
            models.Index(
                fields=["match_data", "team", "scored"],
                name="game_tracke_match_d_7f4a4a_idx",
            ),
            # Speeds up per-player season stats.
            models.Index(
                fields=["player", "scored"],
                name="game_tracke_player__e6d0d1_idx",
            ),
            # Speeds up per-match shot timelines.
            models.Index(
                fields=["match_data", "time"],
                name="shot_match_time_idx",
            ),
            # Speeds up scored-event timelines.
            models.Index(
                fields=["match_data", "scored", "time"],
                name="shot_match_scored_time_idx",
            ),
        )

    def __str__(self) -> str:
        """Return the string representation of the shot.

        Returns:
            str: A string representation of the shot.

        """
        return f"Shot {self.id_uuid} - {self.player} - {self.team} - {self.match_part} - {self.scored}"
