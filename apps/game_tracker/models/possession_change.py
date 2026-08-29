"""Changes of possession during a match."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string
from .event_projection import EventProjectionModel


class PossessionChange(EventProjectionModel):
    """A ball loss or interception, optionally attributed to one player."""

    objects: ClassVar[models.Manager[PossessionChange]]

    BALL_LOSS = "ball_loss"
    INTERCEPTION = "interception"
    KIND_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (BALL_LOSS, "Ball loss"),
        (INTERCEPTION, "Interception"),
    ]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="possession_changes",
    )
    match_data_id: str
    match_part: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="possession_changes",
    )
    match_part_id: str
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="possession_changes",
    )
    team_id: str
    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="possession_changes",
        null=True,
        blank=True,
    )
    player_id: str | None
    kind: models.CharField[str, str] = models.CharField(
        max_length=16,
        choices=KIND_CHOICES,
    )
    time: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    class Meta:
        """Keep common match timeline reads index-backed."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["match_data", "time"],
                name="poss_change_match_time_idx",
            ),
            models.Index(
                fields=["match_data", "player", "kind"],
                name="poss_change_player_kind_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return a concise event description."""
        return f"{self.match_data_id} - {self.player_id or 'unknown'} - {self.kind}"
