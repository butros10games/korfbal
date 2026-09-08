"""Team-private observations attached to a match."""

from typing import ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models


class MatchNote(models.Model):
    """A team's note with author-owned edits and optimistic concurrency."""

    objects: ClassVar[models.Manager["MatchNote"]]
    author_id: int | None

    id = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    match = models.ForeignKey(
        "schedule.Match", on_delete=models.CASCADE, related_name="notes"
    )
    team = models.ForeignKey("team.Team", on_delete=models.CASCADE)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    text = models.TextField(max_length=4000)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep each team's chronological list efficient."""

        indexes: ClassVar = [models.Index(fields=["match", "team", "-created_at"])]

    def __str__(self) -> str:
        """Describe the note without revealing private content."""
        return f"Match note {self.pk}"
