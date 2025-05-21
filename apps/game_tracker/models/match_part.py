"""Model for a part of a match."""

from django.db import models
from uuidv7 import uuid7


class MatchPart(models.Model):
    """Model for a part of a match."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True, default=uuid7, editable=False
    )
    match_data: models.ForeignKey = models.ForeignKey(
        "MatchData", on_delete=models.CASCADE, related_name="match_parts"
    )
    part_number: models.IntegerField = models.IntegerField()
    start_time: models.DateTimeField = models.DateTimeField()
    end_time: models.DateTimeField = models.DateTimeField(blank=True, null=True)
    active: models.BooleanField = models.BooleanField(default=False)

    def __str__(self) -> str:
        """Return the string representation of the match part."""
        return f"Match Part {self.id_uuid} - {self.match_data} - {self.part_number} - {self.start_time} - {self.end_time}"  # noqa: E501
