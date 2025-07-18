"""Module contains the TeamData model."""

from typing import TYPE_CHECKING

from django.db import models

from .constants import player_model_string


if TYPE_CHECKING:
    from django.db.models import QuerySet  # noqa: F401
    from schedule.models import Season  # noqa: F401

    from ..models import Team  # noqa: F401


class TeamData(models.Model):
    """Model for the team data."""

    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="team_data")
    coach = models.ManyToManyField(
        player_model_string,
        related_name="team_data_as_coach",
        blank=True,
    )
    players = models.ManyToManyField(
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

    def __str__(self) -> str:
        """Return the string representation of the team data."""
        return str(self.team.name)
