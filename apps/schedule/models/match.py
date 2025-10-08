"""Module contains the Match model."""

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models
from django.urls import reverse

from .constants import team_model_string


class Match(models.Model):
    """Model for Match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    home_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="home_matches",
    )
    away_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="away_matches",
    )
    season: models.ForeignKey[Any, Any] = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="matches",
    )
    start_time: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    class Meta:
        """Meta class for Match model."""

        indexes: ClassVar[list[Any]] = [
            models.Index(fields=["start_time"]),
            models.Index(fields=["home_team"]),
            models.Index(fields=["away_team"]),
            models.Index(fields=["season", "start_time"]),
        ]

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
