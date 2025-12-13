"""Model for PlayerChange."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string


class PlayerChange(models.Model):
    """Model for a player change in a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    player_in: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="player_changes",
        blank=True,
        null=True,
    )
    player_out: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    player_group: models.ForeignKey[Any, Any] = models.ForeignKey(
        "PlayerGroup",
        on_delete=models.CASCADE,
        related_name="player_changes",
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="player_changes",
        blank=True,
        null=True,
    )
    match_part: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="player_changes",
        blank=True,
        null=True,
    )
    time: models.DateTimeField[datetime, datetime | None] = models.DateTimeField(
        default=None,
        blank=True,
        null=True,
    )

    def __str__(self) -> str:
        """Return the string representation of the player change.

        Returns:
            str: A string representation of the player change.

        """
        player_in = str(self.player_in) if self.player_in else "—"
        player_out = str(self.player_out) if self.player_out else "—"
        return (
            f"PlayerChange {self.id_uuid} - {player_in} - {player_out} - "
            f"{self.match_data} - {self.match_part}"
        )
