"""Module contains the MatchPlayer model for the game_tracker app."""

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string


class MatchPlayer(models.Model):
    """Model for a player in a match."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="players",
    )
    team: models.ForeignKey = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="match_players",
    )
    player: models.ForeignKey = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="match_players",
    )

    def __str__(self) -> str:
        """Return the string representation of the match player.

        Returns:
            str: A string representation of the match player.

        """
        return str(self.player)
