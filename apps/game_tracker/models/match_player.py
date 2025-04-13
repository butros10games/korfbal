"""Module contains the MatchPlayer model for the game_tracker app."""

from django.db import models
from uuidv7 import uuid7

from .constants import player_model_string, team_model_string


class MatchPlayer(models.Model):
    """Model for a player in a match."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_data = models.ForeignKey(
        "MatchData", on_delete=models.CASCADE, related_name="players"
    )
    team = models.ForeignKey(
        team_model_string, on_delete=models.CASCADE, related_name="match_players"
    )
    player = models.ForeignKey(
        player_model_string, on_delete=models.CASCADE, related_name="match_players"
    )

    def __str__(self):
        """Return the string representation of the match player."""
        return str(self.player)
