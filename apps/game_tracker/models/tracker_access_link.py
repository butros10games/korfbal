"""Revocable, match/team-scoped tracker invitations."""

from __future__ import annotations

from typing import ClassVar

from django.db import models


class TrackerAccessLink(models.Model):
    """Keep only a digest of the bearer secret; rotation invalidates guest sessions."""

    objects: ClassVar[models.Manager[TrackerAccessLink]]

    match = models.ForeignKey("schedule.Match", on_delete=models.CASCADE)
    team = models.ForeignKey("team.Team", on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()

    class Meta:
        """Allow one current invitation per match and team."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match", "team"], name="unique_tracker_access_match_team"
            )
        ]

    def __str__(self) -> str:
        """Identify a grant without disclosing its digest."""
        return f"Tracker access {self.pk}"
