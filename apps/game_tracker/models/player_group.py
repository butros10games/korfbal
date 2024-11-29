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

    def save(self, *args, **kwargs):
        """
        Save the player group.

        Check if the incomming player/players are in the reserve player group(if this
        player group is not the reserve) connected to the team and matchdata.

        If the incomming player/players is in the reserve player group, remove the
        player/players from the reserve player group.

        If the player/players is not in the reserve player group, raise a validation
        error.
        """
        if self.pk:
            # Retrieve the current list of players from the database
            current_players = set(self.__class__.objects.get(pk=self.pk).players.all())
        else:
            current_players = set()

        new_players = set(self.players.all()) - current_players

        if self.starting_type.name != "Reserve":
            reserve_player_group = self.match_data.player_groups.get(
                starting_type__name="Reserve"
            )
            for player in new_players:
                if player in reserve_player_group.players.all():
                    reserve_player_group.players.remove(player)
                else:
                    raise ValidationError(
                        f"{player} is not in the reserve player group."
                    )

        super().save(*args, **kwargs)

    def __str__(self):
        """Return the string representation of the player group."""
        return f"Player Group {self.id_uuid}"
