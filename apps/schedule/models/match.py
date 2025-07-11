"""Module contains the Match model."""

from bg_uuidv7 import uuidv7
from django.db import models
from django.urls import reverse

from .constants import team_model_string


class Match(models.Model):
    """Model for Match."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True, default=uuidv7, editable=False
    )
    home_team: models.ForeignKey = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="home_matches",
    )
    away_team: models.ForeignKey = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="away_matches",
    )
    season: models.ForeignKey = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="matches",
    )
    start_time: models.DateTimeField = models.DateTimeField()

    def __str__(self) -> str:
        """Return the string representation of the match."""
        return str(self.home_team.name + " - " + self.away_team.name)

    def get_absolute_url(self) -> str:
        """Return the absolute URL of the match."""
        return reverse("match_detail", kwargs={"match_id": self.id_uuid})
