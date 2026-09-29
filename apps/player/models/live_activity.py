"""iOS Live Activity registrations for live match scores.

A Live Activity (Lock Screen banner and Dynamic Island) is started on the phone
and reports a per-activity APNs token. The backend stores that token per user and
match so committed tracker changes can push the new score and clock to the phone
without the app running.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models


class MatchLiveActivity(models.Model):
    """One running Live Activity on one device for one match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    user: models.ForeignKey[Any, Any] = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="live_activities",
    )
    match: models.ForeignKey[Any, Any] = models.ForeignKey(
        "schedule.Match",
        on_delete=models.CASCADE,
        related_name="live_activities",
    )
    # ActivityKit tokens are hex strings; they rotate, so each rotation upserts.
    push_token: models.CharField[str, str] = models.CharField(
        max_length=512, unique=True
    )
    platform: models.CharField[str, str] = models.CharField(
        max_length=16, default="ios"
    )
    is_active: models.BooleanField[bool, bool] = models.BooleanField(default=True)
    last_pushed_revision: models.PositiveBigIntegerField[int, int] = (
        models.PositiveBigIntegerField(default=0)
    )
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now_add=True
    )
    updated_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["match", "is_active"], name="live_activity_match_active_idx"
            ),
        ]

    user_id: int
    match_id: str

    def __str__(self) -> str:
        """Return a readable label for admin/debug output."""
        return (
            f"Live activity {self.id_uuid} (user={self.user_id}, match={self.match_id})"
        )
