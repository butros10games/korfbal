"""Requests from new accounts to be connected to a club."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

from bg_uuidv7 import uuidv7
from django.db import models
from django.db.models import Q


class ClubJoinRequest(models.Model):
    """An account's claim to belong to (or represent) a club, pending review.

    A claimed KNKV identity is only a hint for the reviewer: a person ID is visible
    to teammates and on match forms, so it is linked after approval, never on entry.
    """

    class Kind(models.TextChoices):
        """What the account claims."""

        MEMBER = "member", "Lid"
        ADMIN = "admin", "Clubbeheerder"

    class Status(models.TextChoices):
        """Review state."""

        PENDING = "pending", "In behandeling"
        APPROVED = "approved", "Geaccepteerd"
        REJECTED = "rejected", "Afgewezen"
        WITHDRAWN = "withdrawn", "Ingetrokken"

    class LinkStatus(models.TextChoices):
        """Progress of linking the claimed KNKV identity after approval."""

        NONE = "", "Geen"
        PENDING = "pending", "Wordt gekoppeld"
        LINKED = "linked", "Gekoppeld"
        FAILED = "failed", "Mislukt"

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True, default=uuidv7, editable=False
    )
    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        "player.Player", on_delete=models.CASCADE, related_name="club_join_requests"
    )
    player_id: UUID
    club: models.ForeignKey[Any, Any] = models.ForeignKey(
        "club.Club", on_delete=models.CASCADE, related_name="join_requests"
    )
    club_id: UUID
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        "team.Team",
        on_delete=models.SET_NULL,
        related_name="join_requests",
        blank=True,
        null=True,
    )
    team_id: UUID | None
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_default=Status.PENDING,
    )
    knkv_person_id = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=200, blank=True)
    link_status = models.CharField(
        max_length=16,
        choices=LinkStatus.choices,
        blank=True,
        default=LinkStatus.NONE,
        db_default=LinkStatus.NONE,
    )
    link_error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(blank=True, null=True)
    decided_by: models.ForeignKey[Any, Any] = models.ForeignKey(
        "player.Player",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )

    class Meta:
        """Model metadata."""

        ordering = ("-created_at",)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("player", "club", "kind"),
                condition=Q(status="pending"),
                name="unique_pending_club_join_request",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("club", "status"), name="club_join_club_status_idx"),
        ]

    def __str__(self) -> str:
        """Return a readable summary."""
        return f"{self.player} → {self.club} ({self.kind}, {self.status})"
