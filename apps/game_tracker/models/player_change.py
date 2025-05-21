"""Model for PlayerChange."""

from django.db import models
from uuidv7 import uuid7

from .constants import player_model_string


class PlayerChange(models.Model):
    """Model for a player change in a match."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True, default=uuid7, editable=False
    )
    player_in: models.ForeignKey = models.ForeignKey(
        player_model_string, on_delete=models.CASCADE, related_name="player_changes"
    )
    player_out: models.ForeignKey = models.ForeignKey(
        player_model_string, on_delete=models.CASCADE
    )
    player_group: models.ForeignKey = models.ForeignKey(
        "PlayerGroup", on_delete=models.CASCADE, related_name="player_changes"
    )
    match_data: models.ForeignKey = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="player_changes",
        blank=True,
        null=True,
    )
    match_part: models.ForeignKey = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="player_changes",
        blank=True,
        null=True,
    )
    time: models.DateTimeField = models.DateTimeField(
        default=None, blank=True, null=True
    )

    def __str__(self) -> str:
        """Return the string representation of the player change."""
        return f"PlayerChange {self.id_uuid} - {self.player_in} - {self.player_out} - {self.match_data} - {self.match_part}"  # noqa: E501
