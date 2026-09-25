"""Module contains the MatchGuestPlayer model for the game_tracker app."""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string


class MatchGuestPlayer(models.Model):
    """A club-less player added to one team's selection for a single match.

    The guest is an ordinary account-less ``Player`` so events, stats and
    substitutions keep working; this row scopes lineup eligibility to the match.
    """

    objects: ClassVar[models.Manager[MatchGuestPlayer]]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="guest_players",
    )
    match_data_id: str
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="match_guest_players",
    )
    team_id: str
    player: models.OneToOneField[Any, Any] = models.OneToOneField(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="match_guest",
    )
    player_id: str
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for MatchGuestPlayer."""

        indexes: ClassVar[tuple[models.Index, ...]] = (
            models.Index(fields=["match_data", "team"], name="mgp_match_team_idx"),
        )

    def __str__(self) -> str:
        """Return the string representation of the guest player.

        Returns:
            str: The guest player's display name.

        """
        return str(self.player)
