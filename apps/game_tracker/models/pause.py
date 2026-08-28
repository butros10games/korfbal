"""Model for a pause in a match."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from .event_projection import EventProjectionModel


class Pause(EventProjectionModel):
    """Model for a pause in a match."""

    objects: ClassVar[models.Manager[Pause]]

    class Meta:
        """Meta options for Pause."""

        indexes = (
            models.Index(
                fields=["match_data", "active", "start_time"],
                name="pause_match_active_time_idx",
            ),
            models.Index(
                fields=["match_data", "start_time"],
                name="pause_match_time_idx",
            ),
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match_data"],
                condition=models.Q(active=True),
                name="uniq_active_match_pause",
            ),
            models.CheckConstraint(
                condition=models.Q(end_time__isnull=True)
                | (
                    models.Q(start_time__isnull=False)
                    & models.Q(end_time__gte=models.F("start_time"))
                ),
                name="pause_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(active=False)
                | (
                    models.Q(start_time__isnull=False) & models.Q(end_time__isnull=True)
                ),
                name="active_pause_has_open_start",
            ),
        ]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="pauses",
    )
    match_part: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="pauses",
        blank=True,
        null=True,
    )
    start_time: models.DateTimeField[datetime, datetime | None] = models.DateTimeField(
        default=None,
        blank=True,
        null=True,
    )
    end_time: models.DateTimeField[datetime | None, datetime | None] = (
        models.DateTimeField(blank=True, null=True)
    )
    active: models.BooleanField[bool, bool] = models.BooleanField(default=False)

    def __str__(self) -> str:
        """Return the string representation of the pause.

        Returns:
            str: A string representation of the pause.

        """
        return f"Pause {self.id_uuid} - {self.match_data} - {self.start_time} - {self.end_time}"

    def length(self) -> timedelta:
        """Return the length of the pause.

        Returns:
            timedelta: The length of the pause.

        """
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return timedelta(0)
