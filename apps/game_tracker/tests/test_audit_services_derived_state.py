"""Audit coverage for derived game-tracker service boundaries."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

from django.db import transaction
from django.utils import timezone
import pytest

from apps.game_tracker.models import PlayerMatchMinutes, Shot
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.game_tracker.services import match_minutes
from apps.game_tracker.services.match_scores import (
    compute_scores_for_matchdata_ids,
    persist_matchdata_scores,
)
from apps.game_tracker.services.recompute import schedule_recompute
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db(transaction=True)
def test_recompute_dispatch_is_commit_aware_and_best_effort() -> None:
    """Derived work runs after commit, skips rollback, and cannot abort a write."""
    dispatch = Mock()

    with transaction.atomic():
        schedule_recompute(
            match_data_id="rolled-back",
            countdown_seconds=7,
            dispatch=dispatch,
            task_name="audit_recompute",
        )
        dispatch.assert_not_called()
        transaction.set_rollback(True)

    dispatch.assert_not_called()

    with transaction.atomic():
        schedule_recompute(
            match_data_id="committed",
            countdown_seconds=11,
            dispatch=dispatch,
            task_name="audit_recompute",
        )
        dispatch.assert_not_called()

    dispatch.assert_called_once_with(
        match_data_id="committed",
        countdown_seconds=11,
    )

    failing_dispatch = Mock(side_effect=RuntimeError("queue unavailable"))
    with transaction.atomic():
        schedule_recompute(
            match_data_id="still-commits",
            countdown_seconds=0,
            dispatch=failing_dispatch,
            task_name="audit_recompute",
        )

    failing_dispatch.assert_called_once_with(
        match_data_id="still-commits",
        countdown_seconds=0,
    )


@pytest.mark.django_db
def test_score_computation_isolates_matches_and_ignores_non_scoring_shots() -> None:
    """Score projection counts only scored shots for either participating team."""
    first = create_tracker_match(prefix="Audit scores first")
    second = create_tracker_match(prefix="Audit scores second")
    player = create_tracker_player(username="audit-score-player")
    outsider = create_tracker_match(prefix="Audit scores outsider").home_team

    Shot.objects.bulk_create([
        Shot(
            player=player,
            match_data=first.match_data,
            team=first.home_team,
            scored=True,
            time=timezone.now(),
        ),
        Shot(
            player=player,
            match_data=first.match_data,
            team=first.away_team,
            scored=True,
            time=timezone.now(),
        ),
        Shot(
            player=player,
            match_data=first.match_data,
            team=first.home_team,
            scored=False,
            time=timezone.now(),
        ),
        Shot(
            player=player,
            match_data=first.match_data,
            team=None,
            scored=True,
            time=timezone.now(),
        ),
        Shot(
            player=player,
            match_data=first.match_data,
            team=outsider,
            scored=True,
            time=timezone.now(),
        ),
        Shot(
            player=player,
            match_data=second.match_data,
            team=second.home_team,
            scored=True,
            time=timezone.now(),
        ),
    ])

    first_id = UUID(str(first.match_data.id_uuid))
    second_id = UUID(str(second.match_data.id_uuid))
    scores = compute_scores_for_matchdata_ids([first_id, second_id])

    assert scores == {
        first_id: (1, 1),
        second_id: (1, 0),
    }

    first.match_data.home_score = 99
    first.match_data.away_score = 98
    first.match_data.save(update_fields=["home_score", "away_score"])
    assert persist_matchdata_scores(first.match_data) == (1, 1)
    first.match_data.refresh_from_db()
    assert (first.match_data.home_score, first.match_data.away_score) == (1, 1)


@pytest.mark.django_db
def test_persist_match_minutes_skips_zero_and_idempotently_updates_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minute persistence writes positives and updates rather than duplicating them."""
    tracker = create_tracker_match(prefix="Audit persisted minutes")
    first = create_tracker_player(username="audit-minutes-first")
    second = create_tracker_player(username="audit-minutes-second")
    computed = {
        str(first.id_uuid): 12.34,
        str(second.id_uuid): 0.0,
    }
    monkeypatch.setattr(
        match_minutes,
        "compute_minutes_by_player_id",
        lambda *, match_data: computed,
    )

    assert match_minutes.persist_match_minutes(match_data=tracker.match_data) == 1
    row = PlayerMatchMinutes.objects.get(match_data=tracker.match_data)
    assert row.player == first
    assert row.algorithm_version == LATEST_MATCH_MINUTES_VERSION
    assert row.minutes_played == Decimal("12.34")

    computed[str(first.id_uuid)] = 8.5
    assert match_minutes.persist_match_minutes(match_data=tracker.match_data) == 1
    assert PlayerMatchMinutes.objects.filter(match_data=tracker.match_data).count() == 1
    row.refresh_from_db()
    assert row.minutes_played == Decimal("8.50")
