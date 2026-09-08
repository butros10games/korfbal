"""Module contains the TeamData model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from django.db import models

from .constants import player_model_string


class TeamData(models.Model):
    """Model for the team data."""

    if TYPE_CHECKING:
        team_id: UUID
        season_id: UUID

    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        "Team", on_delete=models.CASCADE, related_name="team_data"
    )
    coach: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        player_model_string,
        related_name="team_data_as_coach",
        blank=True,
    )
    players: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        player_model_string,
        related_name="team_data_as_player",
        blank=True,
    )
    staff = models.ManyToManyField(
        player_model_string, blank=True, related_name="team_data_as_staff"
    )
    season: models.ForeignKey[Any, Any] = models.ForeignKey(
        "schedule.Season",
        on_delete=models.CASCADE,
        related_name="team_data",
    )
    competition: models.CharField[str, str] = models.CharField(
        max_length=255, blank=True
    )
    wedstrijd_sport: models.BooleanField[bool, bool] = models.BooleanField(
        default=False
    )
    team_rank: models.PositiveIntegerField[int, int] = models.PositiveIntegerField(
        default=1
    )
    fallback_goal_song_song_ids: models.JSONField[list[str]] = models.JSONField(
        default=list,
        blank=True,
    )

    class Meta:
        """Meta class for TeamData model."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("team", "season"), name="unique_team_data_per_season"
            )
        ]
        indexes: ClassVar[list[Any]] = [
            models.Index(fields=["team", "season"]),
        ]

    def __str__(self) -> str:
        """Get the string representation of the team data.

        Returns:
            str: The name of the team.

        """
        return str(self.team.name)
