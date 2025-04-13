"""Module contains the Match model."""

from django.db import models
from django.urls import reverse
from uuidv7 import uuid7

from .constants import team_model_string


class Match(models.Model):
    """Model for Match."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
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

    def __str__(self):
        """Return the string representation of the match."""
        return str(self.home_team.name + " - " + self.away_team.name)

    def get_absolute_url(self):
        """Return the absolute URL of the match."""
        return reverse("match_detail", kwargs={"match_id": self.id_uuid})
