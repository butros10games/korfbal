"""Persisted minutes-played per player per match.

Computing minutes-played requires reconstructing per-match role timelines.
That work is expensive and should not run on read-heavy endpoints.

We persist per-player minutes once (or recompute in the background when match
timeline data changes), and then aggregate cheaply for team/season views.

"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string


LATEST_MATCH_MINUTES_VERSION = "v1"


class PlayerMatchMinutes(models.Model):
    """Stores computed minutes played per player for a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="player_minutes",
    )

    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="match_minutes",
    )

    algorithm_version: models.CharField = models.CharField(
        max_length=32,
        default=LATEST_MATCH_MINUTES_VERSION,
    )

    minutes_played: models.DecimalField = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    computed_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        constraints: ClassVar[tuple[models.BaseConstraint, ...]] = (
            models.UniqueConstraint(
                fields=["match_data", "player", "algorithm_version"],
                name="uniq_player_match_minutes",
            ),
        )
        indexes: ClassVar[tuple[models.Index, ...]] = (
            models.Index(fields=["match_data"], name="minutes_match_idx"),
            models.Index(fields=["player"], name="minutes_player_idx"),
            models.Index(
                fields=["player", "algorithm_version", "match_data"],
                name="minutes_pl_ver_md_idx",
            ),
        )

    def __str__(self) -> str:
        """Return a human-friendly representation."""
        return f"Minutes {self.player} @ {self.match_data}: {self.minutes_played}"
