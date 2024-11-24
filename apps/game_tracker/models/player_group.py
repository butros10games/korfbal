"""This module contains the PlayerGroup model."""

from uuidv7 import uuid7

from django.core.exceptions import ValidationError
from django.db import models

from .constants import player_model_string, team_model_string


class PlayerGroup(models.Model):
    """Model for a group of players in a match."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    players = models.ManyToManyField(
        player_model_string, related_name="player_groups", blank=True
    )
    team = models.ForeignKey(
        team_model_string, on_delete=models.CASCADE, related_name="player_groups"
    )
    match_data = models.ForeignKey(
        "MatchData", on_delete=models.CASCADE, related_name="player_groups"
    )
    starting_type = models.ForeignKey(
        "GroupType", on_delete=models.CASCADE, related_name="player_groups"
    )
    current_type = models.ForeignKey(
        "GroupType", on_delete=models.CASCADE, related_name="current_player_groups"
    )

    def clean(self):
        """Validate the player group."""
        # Ensure that all selected players are part of the match's players field
        valid_players = self.match_data.players.all()
        invalid_players = self.players.exclude(id__in=valid_players)

        if invalid_players.exists():
            raise ValidationError(
                f"""Invalid players selected:
                {', '.join([str(player) for player in invalid_players])}.
                Players must be part of the match data."""
            )

    def __str__(self):
        """Return the string representation of the player group."""
        return f"Player Group {self.id_uuid}"
