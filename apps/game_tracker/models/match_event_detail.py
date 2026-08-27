"""Canonical relational details attached to append-only match events."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import models

from .constants import player_model_string, team_model_string


class ShotEventDetail(models.Model):
    """Canonical shot semantics for one immutable event version."""

    objects: ClassVar[models.Manager[ShotEventDetail]]

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
    event_id: str
    shooting_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_shot_events",
        null=True,
        blank=True,
    )
    shooting_team_id: str | None
    shooter: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_shots_taken",
        null=True,
        blank=True,
    )
    shooter_id: str | None
    defender: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_shots_defended",
        null=True,
        blank=True,
    )
    defender_id: str | None
    shot_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GoalType",
        on_delete=models.SET_NULL,
        related_name="canonical_shot_events",
        null=True,
        blank=True,
    )
    shot_type_id: str | None
    outcome: models.CharField[str, str] = models.CharField(
        max_length=8,
        choices=OUTCOME_CHOICES,
    )

    def __str__(self) -> str:
        """Return the event id and outcome."""
        return f"{self.event_id}: {self.outcome}"


class SubstitutionEventDetail(models.Model):
    """Canonical substitution semantics for one immutable event version."""

    objects: ClassVar[models.Manager[SubstitutionEventDetail]]

    event: models.OneToOneField[Any, Any] = models.OneToOneField(
        "MatchEvent",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="substitution_detail",
    )
    event_id: str
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_substitution_events",
        null=True,
        blank=True,
    )
    team_id: str | None
    player_out: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_substitutions_out",
        null=True,
        blank=True,
    )
    player_out_id: str | None
    player_in: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.SET_NULL,
        related_name="canonical_substitutions_in",
        null=True,
        blank=True,
    )
    player_in_id: str | None
    player_group: models.ForeignKey[Any, Any] = models.ForeignKey(
        "PlayerGroup",
        on_delete=models.SET_NULL,
        related_name="canonical_substitution_events",
        null=True,
        blank=True,
    )
    player_group_id: str | None

    def __str__(self) -> str:
        """Return the event id and player transition."""
        return f"{self.event_id}: {self.player_out_id} -> {self.player_in_id}"
