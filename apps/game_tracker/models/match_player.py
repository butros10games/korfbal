"""Module contains the MatchPlayer model for the game_tracker app."""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string


class MatchPlayer(models.Model):
    """Model for a player in a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="players",
    )
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="match_players",
    )
    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="match_players",
    )

    class Meta:
        """Meta options for MatchPlayer."""

        indexes: ClassVar[tuple[models.Index, ...]] = (
            models.Index(fields=["team", "match_data"], name="mp_team_match_idx"),
            models.Index(fields=["match_data", "player"], name="mp_match_player_idx"),
        )

    def __str__(self) -> str:
        """Return the string representation of the match player.

        Returns:
            str: A string representation of the match player.

        """
        return str(self.player)
