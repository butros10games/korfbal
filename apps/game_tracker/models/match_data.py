"""Model for MatchData."""

from bg_uuidv7 import uuidv7
from django.db import models


class MatchData(models.Model):
    """Model for MatchData."""

    STATUS_CHOICES = [
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

    def __str__(self) -> str:
        """Return the string representation of the match."""
        return str(
            self.match_link.home_team.name + " - " + self.match_link.away_team.name,
        )

    def get_winner(self) -> str | None:
        """Return the winner of the match."""
        if self.home_score > self.away_score:
            return self.match_link.home_team.name
        if self.home_score < self.away_score:
            return self.match_link.away_team.name
        return None
