"""Persistent per-player impact breakdown per match.

This table stores the category breakdown for a computed `PlayerMatchImpact` row.
The Team-page breakdown endpoint can then fetch and aggregate these rows without
recomputing match timelines.

We intentionally store the per-player breakdown only (not the full match dict)
so that the primary lookup path is:

- match_data + player + algorithm_version -> PlayerMatchImpact
- PlayerMatchImpact -> PlayerMatchImpactBreakdown (1:1)

"""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models


class PlayerMatchImpactBreakdown(models.Model):
    """Stores precomputed impact breakdown (per category) for one player+match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    impact: models.OneToOneField[Any, Any] = models.OneToOneField(
        "PlayerMatchImpact",
        on_delete=models.CASCADE,
        related_name="breakdown",
    )

    # Redundant with impact.algorithm_version, but stored here to allow filtering
    # without always joining the impact table.
    algorithm_version: models.CharField = models.CharField(
        max_length=32,
        default="v1",
    )

    # JSON structure: {"<category>": {"points": float, "count": int}, ...}
    breakdown: models.JSONField = models.JSONField(default=dict)

    computed_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        indexes: ClassVar[tuple[models.Index, ...]] = (
            models.Index(fields=["algorithm_version"], name="impact_bd_ver_idx"),
        )

    def __str__(self) -> str:
        """Return a readable label for admin/debugging."""
        impact_id = getattr(self, "impact_id", None)
        return f"Impact breakdown {impact_id} ({self.algorithm_version})"
