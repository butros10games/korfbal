"""Canonical relational details attached to append-only match events."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import models

from .constants import player_model_string, team_model_string


class ShotEventDetail(models.Model):
    """Canonical shot semantics for one immutable event version."""

    OUTCOME_GOAL = "goal"
    OUTCOME_MISS = "miss"
    OUTCOME_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (OUTCOME_GOAL, "Goal"),
        (OUTCOME_MISS, "Miss"),
    ]

    event: models.OneToOneField[Any, Any] = models.OneToOneField(
        "MatchEvent",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="shot_detail",
    )
    shooting_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_shot_events",
        null=True,
        blank=True,
    )
    shooter: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_shots_taken",
        null=True,
        blank=True,
    )
    defender: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_shots_defended",
        null=True,
        blank=True,
    )
    shot_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GoalType",
        on_delete=models.SET_NULL,
        related_name="canonical_shot_events",
        null=True,
        blank=True,
    )
    outcome: models.CharField[str, str] = models.CharField(
        max_length=8,
        choices=OUTCOME_CHOICES,
    )

    def __str__(self) -> str:
        """Return the event id and outcome."""
        return f"{self.event_id}: {self.outcome}"


class SubstitutionEventDetail(models.Model):
    """Canonical substitution semantics for one immutable event version."""

    event: models.OneToOneField[Any, Any] = models.OneToOneField(
        "MatchEvent",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="substitution_detail",
    )
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_substitution_events",
        null=True,
        blank=True,
    )
    player_out: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_substitutions_out",
        null=True,
        blank=True,
    )
    player_in: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_substitutions_in",
        null=True,
        blank=True,
    )
    player_group: models.ForeignKey[Any, Any] = models.ForeignKey(
        "PlayerGroup",
        on_delete=models.SET_NULL,
        related_name="canonical_substitution_events",
        null=True,
        blank=True,
    )

    def __str__(self) -> str:
        """Return the event id and player transition."""
        return f"{self.event_id}: {self.player_out_id} -> {self.player_in_id}"
