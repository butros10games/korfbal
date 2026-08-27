"""Immutable starting-lineup assignment for a tracked match."""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string


class StartingPlayerAssignment(models.Model):
    """Records the group in which a player started the match."""

    objects: ClassVar[models.Manager[StartingPlayerAssignment]]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="starting_player_assignments",
    )
    match_data_id: str
    player_group: models.ForeignKey[Any, Any] = models.ForeignKey(
        "PlayerGroup",
        on_delete=models.CASCADE,
        related_name="starting_player_assignments",
    )
    player_group_id: str
    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="starting_match_assignments",
    )
    player_id: str

    class Meta:
        """Keep one immutable starting group per player and match."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match_data", "player"],
                name="game_tracker_unique_starting_player",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["match_data", "player_group"],
                name="starting_assignment_group_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return a concise starting assignment."""
        return f"{self.match_data_id}:{self.player_id}->{self.player_group_id}"
