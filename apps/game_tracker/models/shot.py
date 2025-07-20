"""Module contains the Shot model for the game_tracker app."""

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string


class Shot(models.Model):
    """Model for a shot in a match."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    player: models.ForeignKey = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="shots",
    )
    match_data: models.ForeignKey = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="shots",
    )
    match_part: models.ForeignKey = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    team: models.ForeignKey = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    for_team: models.BooleanField = models.BooleanField(default=True)
    scored: models.BooleanField = models.BooleanField(default=False)
    shot_type: models.ForeignKey = models.ForeignKey(
        "GoalType",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    time: models.DateTimeField = models.DateTimeField(
        default=None,
        blank=True,
        null=True,
    )

    def __str__(self) -> str:
        """Return the string representation of the shot.

        Returns:
            str: A string representation of the shot.

        """
        return f"Shot {self.id_uuid} - {self.player} - {self.team} - {self.match_part} - {self.scored}"  # noqa: E501
