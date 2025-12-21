"""Persistent per-player impact scores per match.

This table is the foundation for making season/team impact totals identical to the
Match page "impact" algorithm:
- compute impact per match using the same rules
- store per-player per-match values once
- season/team views aggregate (SUM) these stored rows
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string, team_model_string


class PlayerMatchImpact(models.Model):
    """Stores precomputed match impact per player.

    Notes:
        We store `impact_score` rounded to one decimal place to mirror the
        korfbal-web Match page display.

    """

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="player_impacts",
    )

    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="match_impacts",
    )

    # Cached for fast team/season aggregations.
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="player_match_impacts",
    )

    impact_score: models.DecimalField = models.DecimalField(
        max_digits=7,
        decimal_places=1,
        default=Decimal("0.0"),
    )

    algorithm_version: models.CharField = models.CharField(
        max_length=32,
        default="v1",
    )

    computed_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        constraints: ClassVar[tuple[models.BaseConstraint, ...]] = (
            models.UniqueConstraint(
                fields=["match_data", "player"],
                name="uniq_player_match_impact",
            ),
        )
        indexes: ClassVar[tuple[models.Index, ...]] = (
            models.Index(fields=["match_data"], name="impact_match_idx"),
            models.Index(fields=["team"], name="impact_team_idx"),
            models.Index(fields=["player"], name="impact_player_idx"),
            # Common read path: aggregate impacts for player(s) within a match
            # dataset, scoped to the current algorithm version.
            models.Index(
                fields=["player", "algorithm_version", "match_data"],
                name="impact_pl_ver_md_idx",
            ),
        )

    def __str__(self) -> str:
        """Return a human-friendly representation."""
        return f"Impact {self.player} @ {self.match_data}: {self.impact_score}"
