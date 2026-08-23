"""Model for a part of a match."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .event_projection import EventProjectionModel


class MatchPart(EventProjectionModel):
    """Model for a part of a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="match_parts",
    )
    part_number: models.IntegerField[int, int] = models.IntegerField()
    start_time: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    end_time: models.DateTimeField[datetime, datetime | None] = models.DateTimeField(
        blank=True, null=True
    )
    active: models.BooleanField[bool, bool] = models.BooleanField(default=False)

    class Meta:
        """Protect the timer from duplicate or overlapping periods."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match_data", "part_number"],
                name="uniq_match_part_number",
            ),
            models.UniqueConstraint(
                fields=["match_data"],
                condition=models.Q(active=True),
                name="uniq_active_match_part",
            ),
            models.CheckConstraint(
                condition=models.Q(end_time__isnull=True)
                | models.Q(end_time__gte=models.F("start_time")),
                name="match_part_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        """Return the string representation of the match part.

        Returns:
            str: A string representation of the match part.

        """
        return f"Match Part {self.id_uuid} - {self.match_data} - {self.part_number} - {self.start_time} - {self.end_time}"
