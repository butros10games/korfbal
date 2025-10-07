"""Model for MatchData."""

from typing import ClassVar

from bg_uuidv7 import uuidv7
from django.db import models


class MatchData(models.Model):
    """Model for MatchData."""

    STATUS_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("upcoming", "Upcoming"),
        ("active", "Active"),
        ("finished", "Finished"),
    ]

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_link: models.ForeignKey = models.ForeignKey(
        "schedule.Match",
        on_delete=models.CASCADE,
    )
    home_score: models.IntegerField = models.IntegerField(default=0)
    away_score: models.IntegerField = models.IntegerField(default=0)
    parts: models.IntegerField = models.IntegerField(default=2)
    current_part: models.IntegerField = models.IntegerField(default=1)
    part_length: models.IntegerField = models.IntegerField(default=1800)
    status: models.CharField = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default="upcoming",
    )

    class Meta:
        """Meta class for MatchData model."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["status"]),
            models.Index(fields=["match_link"]),
            models.Index(fields=["status", "match_link"]),
        ]

    def __str__(self) -> str:
        """Return the string representation of the match.

        Returns:
            str: A string representation of the match.

        """
        return str(
            self.match_link.home_team.name + " - " + self.match_link.away_team.name,
        )

    def get_winner(self) -> str | None:
        """Determine the winner of the match.

        Returns:
            str | None: The name of the winning team, or None if it's a draw.

        """
        if self.home_score > self.away_score:
            return self.match_link.home_team.name
        if self.home_score < self.away_score:
            return self.match_link.away_team.name
        return None
