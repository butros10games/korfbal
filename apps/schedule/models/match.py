"""Module contains the Match model."""

from bg_uuidv7 import uuidv7
from django.db import models
from django.urls import reverse

from .constants import team_model_string


class Match(models.Model):
    """Model for Match."""

    id_uuid = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    home_team = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="home_matches",
    )
    away_team = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="away_matches",
    )
    season = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="matches",
    )
    start_time = models.DateTimeField()

    def __str__(self) -> str:
        """Get the string representation of the match.

        Returns:
            str: The names of the home and away teams.

        """
        return str(self.home_team.name + " - " + self.away_team.name)

    def get_absolute_url(self) -> str:
        """Get the absolute URL for the match detail view.

        Returns:
            str: The URL to the match detail view.

        """
        return reverse("match_detail", kwargs={"match_id": self.id_uuid})
