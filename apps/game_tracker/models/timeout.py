"""Module contains the Timeout model for the game_tracker app."""

from __future__ import annotations

from typing import Any

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import team_model_string


class Timeout(models.Model):
    """Model for a timeout in a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="timeouts",
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
    pause: models.ForeignKey[Any, Any] = models.ForeignKey(
        "Pause",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )

    def __str__(self) -> str:
        """Return the string representation of the timeout.

        Returns:
            str: A string representation of the timeout.

        """
        return f"Timeout {self.id_uuid} - {self.match_data} - {self.team}"
