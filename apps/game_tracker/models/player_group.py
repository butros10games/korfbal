"""Module contains the PlayerGroup model."""

from __future__ import annotations

from typing import Any

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string


class PlayerGroup(models.Model):
    """Model for a group of players in a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    players: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        player_model_string,
        related_name="player_groups",
        blank=True,
    )
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="player_groups",
    )
    team_id: str
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="player_groups",
    )
    match_data_id: str
    starting_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GroupType",
        on_delete=models.CASCADE,
        related_name="player_groups",
    )
    current_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GroupType",
        on_delete=models.CASCADE,
        related_name="current_player_groups",
    )

    def __str__(self) -> str:
        """Return the string representation of the player group.

        Returns:
            str: A string representation of the player group.

        """
        return f"Player Group {self.id_uuid} - {self.team} - {self.match_data} - {self.starting_type} - {self.current_type}"
