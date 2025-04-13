"""Module contains the Timeout model for the game_tracker app."""

from django.db import models
from uuidv7 import uuid7

from .constants import team_model_string


class Attack(models.Model):
    """Model for a timeout in a match."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_data = models.ForeignKey(
        "MatchData", on_delete=models.CASCADE, related_name="attacks"
    )
    match_part = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    team = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    time = models.DateTimeField(default=None, blank=True, null=True)
