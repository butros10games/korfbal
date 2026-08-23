"""Cross-team canonical event reconciliation coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    GoalType,
    MatchEvent,
    MatchEventObservation,
    MatchEventReconciliation,
    MatchEventReconciliationDecision,
    MatchPart,
    Shot,
)
from apps.game_tracker.services.event_reconciliation import (
    ReconciliationResolution,
    pending_reconciliations,
    resolve_reconciliation,
)
from apps.game_tracker.services.tracker_http import apply_tracker_command
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_group_types,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import Player


TWO_REPORTS = 2
THREE_AMBIGUOUS_SHOTS = 3


@dataclass(frozen=True, slots=True)
class ReconciliationTracker:
    """Minimal active tracker with one player on each side."""

    tracker: TrackerMatchContext
    part: MatchPart
    goal_type: GoalType
    home_player: Player
    away_player: Player


def _active_tracker(prefix: str) -> ReconciliationTracker:
    tracker = create_tracker_match(prefix=prefix)
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    part = MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=timezone.now() - timedelta(minutes=2),
        active=True,
    )
    goal_type = GoalType.objects.create(name=f"{prefix} goal")
    group_types = create_group_types(
        f"{prefix} Aanval",
        f"{prefix} Verdediging",
    )
    home_player = create_tracker_player(username=f"{prefix}-home-player")
    away_player = create_tracker_player(username=f"{prefix}-away-player")
    home_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types[f"{prefix} Aanval"],
    )
    away_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.away_team,
        group_type=group_types[f"{prefix} Verdediging"],
    )
    home_group.players.add(home_player)
    away_group.players.add(away_player)
    return ReconciliationTracker(
        tracker=tracker,
        part=part,
        goal_type=goal_type,
        home_player=home_player,
        away_player=away_player,
    )


def _goal_payload(
    *,
    player: Player,
    goal_type: GoalType,
    for_team: bool,
    observed_at_ms: int,
) -> dict[str, object]:
    return {
        "command": "goal_reg",
        "command_id": str(uuid4()),
        "player_id": str(player.pk),
        "goal_type": str(goal_type.pk),
        "for_team": for_team,
        "client_time_ms": observed_at_ms,
    }


@pytest.mark.django_db
def test_opposing_team_reports_attach_to_one_canonical_goal() -> None:
    """Two perspectives on one close goal change the score only once."""
    context = _active_tracker("reconcile-one")
    observed_at = timezone.now()

    apply_tracker_command(
        context.tracker.match,
        team=context.tracker.home_team,
        payload=_goal_payload(
            player=context.home_player,
            goal_type=context.goal_type,
            for_team=True,
            observed_at_ms=int(observed_at.timestamp() * 1_000),
        ),
    )
    state = apply_tracker_command(
        context.tracker.match,
        team=context.tracker.away_team,
        payload=_goal_payload(
            player=context.away_player,
            goal_type=context.goal_type,
            for_team=False,
            observed_at_ms=int(
                (observed_at + timedelta(seconds=1)).timestamp() * 1_000
            ),
        ),
    )

    shot_event = MatchEvent.objects.get(
        match_data=context.tracker.match_data,
        source_type="shot",
    )
    observations = MatchEventObservation.objects.filter(event=shot_event).order_by(
        "recorded_at"
    )
    assert Shot.objects.filter(match_data=context.tracker.match_data).count() == 1
    assert observations.count() == TWO_REPORTS
    assert set(observations.values_list("reporting_team_id", flat=True)) == {
        context.tracker.home_team.pk,
        context.tracker.away_team.pk,
    }
    assert observations.last().origin == MatchEventObservation.ORIGIN_MATCHED
    assert state["score"] == {"for": 0, "against": 1}


@pytest.mark.django_db
def test_same_team_reports_remain_distinct_events() -> None:
    """Temporal proximity never combines two actions reported by one team."""
    context = _active_tracker("reconcile-same-team")
    observed_at = timezone.now()
    for offset in (0, 1):
        apply_tracker_command(
            context.tracker.match,
            team=context.tracker.home_team,
            payload=_goal_payload(
                player=context.home_player,
                goal_type=context.goal_type,
                for_team=True,
                observed_at_ms=int(
                    (observed_at + timedelta(seconds=offset)).timestamp() * 1_000
                ),
            ),
        )

    assert (
        Shot.objects.filter(match_data=context.tracker.match_data).count()
        == TWO_REPORTS
    )
    assert (
        MatchEvent.objects.filter(
            match_data=context.tracker.match_data, source_type="shot"
        ).count()
        == TWO_REPORTS
    )


@pytest.mark.django_db
def test_ambiguous_cross_team_reports_create_review_candidates() -> None:
    """Multiple close facts are preserved for a human decision, never guessed."""
    context = _active_tracker("reconcile-review")
    observed_at = timezone.now()
    for offset in (0, 4):
        apply_tracker_command(
            context.tracker.match,
            team=context.tracker.home_team,
            payload=_goal_payload(
                player=context.home_player,
                goal_type=context.goal_type,
                for_team=True,
                observed_at_ms=int(
                    (observed_at + timedelta(seconds=offset)).timestamp() * 1_000
                ),
            ),
        )
    apply_tracker_command(
        context.tracker.match,
        team=context.tracker.away_team,
        payload=_goal_payload(
            player=context.away_player,
            goal_type=context.goal_type,
            for_team=False,
            observed_at_ms=int(
                (observed_at + timedelta(seconds=2)).timestamp() * 1_000
            ),
        ),
    )

    candidates = MatchEventReconciliation.objects.filter(
        match_data=context.tracker.match_data
    )
    assert (
        Shot.objects.filter(match_data=context.tracker.match_data).count()
        == THREE_AMBIGUOUS_SHOTS
    )
    assert candidates.count() == TWO_REPORTS
    assert all(candidate.confidence > 0 for candidate in candidates)
    assert not any(hasattr(candidate, "decision") for candidate in candidates)


@pytest.mark.django_db
def test_manual_merge_retracts_duplicate_and_rebuilds_score() -> None:
    """A reviewed merge keeps one score fact and an immutable decision event."""
    context = _active_tracker("reconcile-merge")
    observed_at = timezone.now()
    apply_tracker_command(
        context.tracker.match,
        team=context.tracker.home_team,
        payload=_goal_payload(
            player=context.home_player,
            goal_type=context.goal_type,
            for_team=True,
            observed_at_ms=int(observed_at.timestamp() * 1_000),
        ),
    )
    apply_tracker_command(
        context.tracker.match,
        team=context.tracker.away_team,
        payload=_goal_payload(
            player=context.away_player,
            goal_type=context.goal_type,
            for_team=False,
            observed_at_ms=int(
                (observed_at + timedelta(seconds=5)).timestamp() * 1_000
            ),
        ),
    )
    candidate = MatchEventReconciliation.objects.get(
        match_data=context.tracker.match_data
    )

    result = resolve_reconciliation(
        ReconciliationResolution(
            match_data=context.tracker.match_data,
            reconciliation_id=candidate.pk,
            decision=MatchEventReconciliationDecision.DECISION_MERGE,
            canonical_event_id=candidate.first_event_id,
            actor=None,
            reason="Both scorers confirmed the same goal.",
        )
    )

    context.tracker.match_data.refresh_from_db()
    assert result.canonical_event_id == candidate.first_event_id
    assert result.resolution_event.kind == "reconciliation.merge"
    assert Shot.objects.filter(match_data=context.tracker.match_data).count() == 1
    assert context.tracker.match_data.home_score == 1
    assert context.tracker.match_data.away_score == 0
    assert pending_reconciliations(context.tracker.match_data) == []


@pytest.mark.django_db
def test_manual_separate_decision_preserves_both_events() -> None:
    """Review can explicitly retain two close but distinct real-world goals."""
    context = _active_tracker("reconcile-separate")
    observed_at = timezone.now()
    apply_tracker_command(
        context.tracker.match,
        team=context.tracker.home_team,
        payload=_goal_payload(
            player=context.home_player,
            goal_type=context.goal_type,
            for_team=True,
            observed_at_ms=int(observed_at.timestamp() * 1_000),
        ),
    )
    apply_tracker_command(
        context.tracker.match,
        team=context.tracker.away_team,
        payload=_goal_payload(
            player=context.away_player,
            goal_type=context.goal_type,
            for_team=False,
            observed_at_ms=int(
                (observed_at + timedelta(seconds=5)).timestamp() * 1_000
            ),
        ),
    )
    candidate = MatchEventReconciliation.objects.get(
        match_data=context.tracker.match_data
    )

    result = resolve_reconciliation(
        ReconciliationResolution(
            match_data=context.tracker.match_data,
            reconciliation_id=candidate.pk,
            decision=MatchEventReconciliationDecision.DECISION_SEPARATE,
            canonical_event_id=None,
            actor=None,
            reason="Two separate goals.",
        )
    )

    assert result.canonical_event is None
    assert result.resolution_event.kind == "reconciliation.separate"
    assert (
        Shot.objects.filter(match_data=context.tracker.match_data).count()
        == TWO_REPORTS
    )
    assert pending_reconciliations(context.tracker.match_data) == []
