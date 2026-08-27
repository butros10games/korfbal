"""Durable per-revision metadata for live match synchronization."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import models


class MatchLiveChange(models.Model):
    """Records which client resources changed at a match revision."""

    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="live_changes",
    )
    revision: models.PositiveBigIntegerField[int, int] = (
        models.PositiveBigIntegerField()
    )
    resources: models.JSONField[list[str], list[str]] = models.JSONField(default=list)
    changed_ids: models.JSONField[dict[str, list[str]], dict[str, list[str]]] = (
        models.JSONField(default=dict)
    )
    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Database constraints and lookup indexes."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match_data", "revision"],
                name="game_tracker_unique_live_revision",
            ),
        ]

    def __str__(self) -> str:
        """Return a concise revision identifier."""
        return f"{self.match_data.pk}@{self.revision}"
