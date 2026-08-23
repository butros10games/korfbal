"""Immutable reconciliation candidates and their decisions."""

from __future__ import annotations

from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models


class MatchEventReconciliation(models.Model):
    """A possible duplicate pair discovered from independent observations."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="event_reconciliations",
    )
    first_event: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchEvent",
        on_delete=models.CASCADE,
        related_name="reconciliations_as_first",
    )
    second_event: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchEvent",
        on_delete=models.CASCADE,
        related_name="reconciliations_as_second",
    )
    match_data_id: str
    first_event_id: str
    second_event_id: str
    confidence: models.PositiveSmallIntegerField[int, int] = (
        models.PositiveSmallIntegerField()
    )
    reason: models.CharField[str, str] = models.CharField(max_length=255)
    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Store each normalized event pair once."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["first_event", "second_event"],
                name="game_tracker_unique_reconciliation_pair",
            ),
            models.CheckConstraint(
                condition=~models.Q(first_event=models.F("second_event")),
                name="game_tracker_distinct_reconciliation_events",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["match_data", "-created_at"],
                name="reconcile_match_time_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return the normalized candidate pair."""
        return f"{self.first_event_id} ~ {self.second_event_id}"


class MatchEventReconciliationDecision(models.Model):
    """One append-only decision resolving a duplicate candidate."""

    DECISION_MERGE = "merge"
    DECISION_SEPARATE = "separate"
    DECISION_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (DECISION_MERGE, "Merge"),
        (DECISION_SEPARATE, "Keep separate"),
    ]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    reconciliation: models.OneToOneField[Any, Any] = models.OneToOneField(
        MatchEventReconciliation,
        on_delete=models.CASCADE,
        related_name="decision",
    )
    decision: models.CharField[str, str] = models.CharField(
        max_length=8,
        choices=DECISION_CHOICES,
    )
    canonical_event: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchEvent",
        on_delete=models.PROTECT,
        related_name="reconciliation_decisions",
        null=True,
        blank=True,
    )
    resolution_event: models.OneToOneField[Any, Any] = models.OneToOneField(
        "MatchEvent",
        on_delete=models.PROTECT,
        related_name="reconciliation_resolution",
    )
    reconciliation_id: str
    canonical_event_id: str | None
    resolution_event_id: str
    actor: models.ForeignKey[Any, Any] = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="match_event_reconciliation_decisions",
        null=True,
        blank=True,
    )
    reason: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Decisions are immutable once inserted."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    models.Q(decision="merge", canonical_event__isnull=False)
                    | models.Q(decision="separate", canonical_event__isnull=True)
                ),
                name="game_tracker_valid_reconciliation_decision",
            ),
        ]

    def __str__(self) -> str:
        """Return the candidate and decision."""
        return f"{self.reconciliation_id}: {self.decision}"
