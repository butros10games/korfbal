"""Module contains the TeamData model."""

from typing import ClassVar

from django.db import models

from .constants import player_model_string


class TeamData(models.Model):
    """Model for the team data."""

    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="team_data")
    coach: models.ManyToManyField = models.ManyToManyField(  # type: ignore[type-arg]
        player_model_string,
        related_name="team_data_as_coach",
        blank=True,
    )
    players: models.ManyToManyField = models.ManyToManyField(  # type: ignore[type-arg]
        player_model_string,
        related_name="team_data_as_player",
        blank=True,
    )
    season = models.ForeignKey(
        "schedule.Season",
        on_delete=models.CASCADE,
        related_name="team_data",
    )
    competition = models.CharField(max_length=255, blank=True)

    class Meta:
        """Meta class for TeamData model."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["team"]),
            models.Index(fields=["season"]),
            models.Index(fields=["team", "season"]),
        ]

    def __str__(self) -> str:
        """Get the string representation of the team data.

        Returns:
            str: The name of the team.

        """
        return str(self.team.name)
