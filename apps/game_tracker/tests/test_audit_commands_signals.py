"""Audit coverage for game-tracker signal transaction boundaries."""

from __future__ import annotations

from unittest.mock import Mock

from django.db import transaction
from django.utils import timezone
import pytest

from apps.game_tracker import composition
from apps.game_tracker.models import MatchLiveChange, Shot
from apps.game_tracker.services.match_event_context import match_data_is_deleting
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db(transaction=True)
def test_finished_transition_dispatches_after_commit_and_not_after_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finished-match jobs are commit-aware and run only on the first transition."""
    impact_dispatch = Mock()
    minutes_dispatch = Mock()
    monkeypatch.setattr(
        composition.tracker_jobs,
        "recompute_impacts",
        impact_dispatch,
    )
    monkeypatch.setattr(
        composition.tracker_jobs,
        "recompute_minutes",
        minutes_dispatch,
    )
    tracker = create_tracker_match(prefix="Finished signal")

    with transaction.atomic():
        tracker.match_data.status = "finished"
        tracker.match_data.save(update_fields=["status"])
        transaction.set_rollback(True)

    impact_dispatch.assert_not_called()
    minutes_dispatch.assert_not_called()
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.status == "upcoming"

    tracker.match_data.status = "finished"
    tracker.match_data.save(update_fields=["status"])

    impact_dispatch.assert_called_once_with(
        match_data_id=str(tracker.match_data.id_uuid),
        countdown_seconds=30,
    )
    minutes_dispatch.assert_called_once_with(
        match_data_id=str(tracker.match_data.id_uuid),
        countdown_seconds=30,
    )

    tracker.match_data.home_score = 1
    tracker.match_data.save(update_fields=["home_score"])
    assert impact_dispatch.call_count == 1
    assert minutes_dispatch.call_count == 1


@pytest.mark.django_db(transaction=True)
def test_realtime_shot_side_effects_roll_back_with_the_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rolled-back shot publishes nothing and leaves no revision history."""
    publish = Mock()
    monkeypatch.setattr(composition.change_publisher, "publish", publish)
    monkeypatch.setattr(
        composition.tracker_jobs,
        "recompute_impacts",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        composition.tracker_jobs,
        "recompute_minutes",
        lambda **_kwargs: None,
    )
    tracker = create_tracker_match(prefix="Realtime rollback")
    player = create_tracker_player(username="realtime-rollback-player")

    with transaction.atomic():
        Shot.objects.create(
            player=player,
            match_data=tracker.match_data,
            team=tracker.home_team,
            scored=False,
            time=timezone.now(),
        )
        assert MatchLiveChange.objects.filter(
            match_data=tracker.match_data,
            revision=1,
        ).exists()
        transaction.set_rollback(True)

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == 0
    assert Shot.objects.filter(match_data=tracker.match_data).exists() is False
    live_changes = MatchLiveChange.objects.filter(match_data=tracker.match_data)
    assert live_changes.exists() is False
    publish.assert_not_called()

    Shot.objects.create(
        player=player,
        match_data=tracker.match_data,
        team=tracker.home_team,
        scored=False,
        time=timezone.now(),
    )

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == 1
    publish.assert_called_once()


@pytest.mark.django_db
def test_match_data_cascade_suppresses_child_realtime_side_effects_and_cleans_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aggregate deletion neither emits child revisions nor leaks deletion state."""
    tracker = create_tracker_match(prefix="Signal cascade")
    player = create_tracker_player(username="signal-cascade-player")
    shot = Shot.objects.create(
        player=player,
        match_data=tracker.match_data,
        team=tracker.home_team,
        scored=False,
        time=timezone.now(),
    )
    record_change = Mock()
    monkeypatch.setattr(
        "apps.game_tracker.signals.realtime_update_signals.record_match_change",
        record_change,
    )
    match_data_id = tracker.match_data.pk

    tracker.match_data.delete()

    record_change.assert_not_called()
    assert Shot.objects.filter(pk=shot.pk).exists() is False
    assert match_data_is_deleting(match_data_id) is False
