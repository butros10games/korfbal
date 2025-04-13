"""Module contains the Shot model for the game_tracker app."""

from django.db import models
from uuidv7 import uuid7

from .constants import player_model_string, team_model_string


class Shot(models.Model):
    """Model for a shot in a match."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player = models.ForeignKey(
        player_model_string, on_delete=models.CASCADE, related_name="shots"
    )
    match_data = models.ForeignKey(
        "MatchData", on_delete=models.CASCADE, related_name="shots"
    )
    match_part = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    team = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    for_team = models.BooleanField(default=True)
    scored = models.BooleanField(default=False)
    shot_type = models.ForeignKey(
        "GoalType",
        on_delete=models.CASCADE,
        related_name="shots",
        blank=True,
        null=True,
    )
    time = models.DateTimeField(default=None, blank=True, null=True)
