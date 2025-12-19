"""Model for historical Player\N{RIGHTWARDS ARROW}Club membership."""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models
from django.db.models import Q
from django.utils import timezone


class PlayerClubMembership(models.Model):
    """Time-bounded membership of a player to a club.

    Why this exists:
        - A player can (rarely) belong to multiple clubs.
        - Players can switch clubs; historical match/team data must not change.

    Notes:
        - We store membership history here; do not delete rows for transfers.
        - Use end_date to close a membership.

    """

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        "player.Player",
        on_delete=models.CASCADE,
        related_name="club_membership_links",
    )
    club: models.ForeignKey[Any, Any] = models.ForeignKey(
        "club.Club",
        on_delete=models.CASCADE,
        related_name="player_membership_links",
    )

    start_date: models.DateField = models.DateField(default=timezone.localdate)
    end_date: models.DateField | None = models.DateField(blank=True, null=True)

    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        indexes: ClassVar[list[Any]] = [
            models.Index(fields=["player"], name="pcm_player_idx"),
            models.Index(fields=["club"], name="pcm_club_idx"),
            models.Index(fields=["player", "club"], name="pcm_player_club_idx"),
            models.Index(fields=["start_date"], name="pcm_start_date_idx"),
            models.Index(fields=["end_date"], name="pcm_end_date_idx"),
        ]
        constraints: ClassVar[list[Any]] = [
            # Ensure end_date is not before start_date (or is NULL).
            models.CheckConstraint(
                condition=Q(end_date__isnull=True)
                | Q(end_date__gte=models.F("start_date")),
                name="playerclubmembership_end_after_start",
            ),
            # Only one *open* membership per (player, club).
            models.UniqueConstraint(
                fields=["player", "club"],
                condition=Q(end_date__isnull=True),
                name="playerclubmembership_unique_active",
            ),
        ]

    def __str__(self) -> str:
        """Return a friendly label for admin/debugging."""
        end = self.end_date.isoformat() if self.end_date else "present"
        return f"{self.player} @ {self.club} ({self.start_date.isoformat()} - {end})"
