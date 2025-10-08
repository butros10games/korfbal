"""Module contains the Timeout model for the game_tracker app."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import team_model_string


class Attack(models.Model):
    """Model for a timeout in a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="attacks",
    )
    match_part: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    time: models.DateTimeField[datetime, datetime | None] = models.DateTimeField(
        default=None,
        blank=True,
        null=True,
    )

    def __str__(self) -> str:
        """Return the string representation of the attack.

        Returns:
            str: A string representation of the attack.

        """
        return f"{self.match_data} - {self.team} - {self.time}"
