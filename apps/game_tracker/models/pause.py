"""Model for a pause in a match."""

from datetime import timedelta

from bg_uuidv7 import uuidv7
from django.db import models


class Pause(models.Model):
    """Model for a pause in a match."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="pauses",
    )
    match_part: models.ForeignKey = models.ForeignKey(
        "MatchPart",
        on_delete=models.CASCADE,
        related_name="pauses",
        blank=True,
        null=True,
    )
    start_time: models.DateTimeField = models.DateTimeField(
        default=None,
        blank=True,
        null=True,
    )
    end_time: models.DateTimeField = models.DateTimeField(blank=True, null=True)
    active: models.BooleanField = models.BooleanField(default=False)

    def __str__(self) -> str:
        """Return the string representation of the pause."""
        return f"Pause {self.id_uuid} - {self.match_data} - {self.start_time} - {self.end_time}"  # noqa: E501

    def length(self) -> timedelta:
        """Return the length of the pause."""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return timedelta(0)
