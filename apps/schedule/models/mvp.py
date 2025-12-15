"""Models for Match MVP voting."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models
from django.db.models import Q


MATCH_MODEL = "schedule.Match"
PLAYER_MODEL = "player.Player"


class MatchMvp(models.Model):
    """Tracks the MVP voting window and published winner for a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    match: models.OneToOneField[Any, Any] = models.OneToOneField(
        MATCH_MODEL,
        on_delete=models.CASCADE,
        related_name="mvp",
    )

    finished_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    closes_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    mvp_player: models.ForeignKey[Any, Any] = models.ForeignKey(
        PLAYER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mvp_awards",
    )
    published_at: models.DateTimeField[datetime, datetime | None] = (
        models.DateTimeField(
            null=True,
            blank=True,
        )
    )

    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        """Meta options."""

        indexes: ClassVar[list[Any]] = [
            models.Index(fields=["match"]),
            models.Index(fields=["closes_at"]),
            models.Index(fields=["published_at"]),
        ]

    # Django creates `<fk_field>_id` attributes dynamically.
    match_id: str
    mvp_player_id: str | None

    def __str__(self) -> str:
        """Return a readable label for admin/debug output."""
        return f"Match MVP for {self.match_id}"


class MatchMvpVote(models.Model):
    """A single voter vote for the MVP of a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    match: models.ForeignKey[Any, Any] = models.ForeignKey(
        MATCH_MODEL,
        on_delete=models.CASCADE,
        related_name="mvp_votes",
    )
    # Authenticated vote: tied to a Player.
    voter: models.ForeignKey[Any, Any] = models.ForeignKey(
        PLAYER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="mvp_votes_cast",
    )

    # Anonymous vote: tied to a signed cookie token.
    voter_token: models.UUIDField[str, str] = models.UUIDField(
        null=True,
        blank=True,
    )

    candidate: models.ForeignKey[Any, Any] = models.ForeignKey(
        PLAYER_MODEL,
        on_delete=models.CASCADE,
        related_name="mvp_votes_received",
    )

    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        """Meta options."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["match", "voter"],
                condition=Q(voter__isnull=False),
                name="unique_mvp_vote_per_match_voter",
            ),
            models.UniqueConstraint(
                fields=["match", "voter_token"],
                condition=Q(voter_token__isnull=False),
                name="unique_mvp_vote_per_match_voter_token",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(voter__isnull=False) & Q(voter_token__isnull=True))
                    | (Q(voter__isnull=True) & Q(voter_token__isnull=False))
                ),
                name="mvp_vote_requires_voter_or_token",
            ),
        ]
        indexes: ClassVar[list[Any]] = [
            models.Index(fields=["match"]),
            models.Index(fields=["candidate"]),
            models.Index(fields=["voter_token"]),
        ]

    # Django creates `<fk_field>_id` attributes dynamically.
    match_id: str
    voter_id: str | None
    candidate_id: str

    def __str__(self) -> str:
        """Return a readable label for admin/debug output."""
        voter_label = str(self.voter_id) if self.voter_id else str(self.voter_token)
        return f"MVP vote: {self.match_id} {voter_label} -> {self.candidate_id}"
