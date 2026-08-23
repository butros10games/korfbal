"""Durable idempotency receipts for committed tracker commands."""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models

from .constants import team_model_string


class TrackerCommand(models.Model):
    """Records the identity and commit order of a tracker state transition."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    command_id: models.UUIDField[str, str] = models.UUIDField()
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="tracker_commands",
    )
    match_data_id: str
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.PROTECT,
        related_name="tracker_commands",
    )
    team_id: str
    actor: models.ForeignKey[Any, Any] = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="tracker_commands",
        null=True,
        blank=True,
    )
    sequence: models.PositiveBigIntegerField[int, int] = (
        models.PositiveBigIntegerField()
    )
    command: models.CharField[str, str] = models.CharField(max_length=40)
    payload_hash: models.CharField[str, str] = models.CharField(max_length=64)
    expected_revision: models.PositiveBigIntegerField[int, int | None] = (
        models.PositiveBigIntegerField(null=True, blank=True)
    )
    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Enforce idempotency and a total command order per match."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match_data", "command_id"],
                name="game_tracker_unique_command_id",
            ),
            models.UniqueConstraint(
                fields=["match_data", "sequence"],
                name="game_tracker_unique_command_sequence",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["match_data", "-sequence"],
                name="tracker_cmd_match_seq_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return a concise command receipt identifier."""
        return f"{self.match_data_id}@{self.sequence}:{self.command}"
