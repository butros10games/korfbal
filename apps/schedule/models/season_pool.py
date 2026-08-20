"""Pools of teams within a season."""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import team_model_string


class SeasonPool(models.Model):
    """A named group of teams that play each other within one season."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    season: models.ForeignKey[Any, Any] = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="pools",
    )
    season_id: str
    name: models.CharField[str, str] = models.CharField(max_length=120)
    teams: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        team_model_string,
        related_name="season_pools",
        blank=True,
    )

    class Meta:
        """Keep pool names unique inside a season."""

        ordering: ClassVar[list[str]] = ["name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["season", "name"],
                name="unique_pool_name_per_season",
            ),
        ]

    def __str__(self) -> str:
        """Return a season-qualified pool label."""
        return f"{self.season.name} · {self.name}"
