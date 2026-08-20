"""Regression coverage for durable live-match revisions."""

from __future__ import annotations

import json

import pytest

from apps.game_tracker.models import MatchData, MatchLiveChange
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.live_updates import (
    record_match_change,
    summarize_match_changes,
)
from apps.game_tracker.services.tracker_http import (
    apply_tracker_command,
    get_tracker_state,
    poll_tracker_state,
)
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


UNDO_REVISION = 3
STALE_WRITERS_REVISION = 2


@pytest.mark.django_db(transaction=True)
def test_tracker_command_commits_one_durable_revision() -> None:
    """A command with several ORM writes advances the match exactly once."""
    tracker = create_tracker_match(prefix="Live revision")

    state = apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == 1
    assert state["live_revision"] == 1


@pytest.mark.django_db(transaction=True)
def test_undo_advances_revision_after_deleting_last_event() -> None:
    """Undo remains observable even though its newest event is deleted."""
    tracker = create_tracker_match(prefix="Live undo")
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "new_attack"},
    )

    state = apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "remove_last_event"},
    )

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == UNDO_REVISION
    assert state["live_revision"] == UNDO_REVISION
    assert state["last_event"] == {"type": "no_event"}


@pytest.mark.django_db(transaction=True)
def test_stale_writers_cannot_overwrite_a_newer_revision() -> None:
    """Two writers loaded at the same revision still produce unique revisions."""
    tracker = create_tracker_match(prefix="Live concurrency")
    first_writer = MatchData.objects.get(pk=tracker.match_data.pk)
    second_writer = MatchData.objects.get(pk=tracker.match_data.pk)

    assert record_match_change(first_writer) == 1
    assert record_match_change(second_writer) == STALE_WRITERS_REVISION

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == STALE_WRITERS_REVISION
    assert list(
        MatchLiveChange.objects
        .filter(match_data=tracker.match_data)
        .order_by("revision")
        .values_list("revision", flat=True)
    ) == [1, STALE_WRITERS_REVISION]


@pytest.mark.django_db(transaction=True)
def test_change_summary_preserves_resources_and_entity_ids() -> None:
    """Fallback pollers can invalidate only the changed datasets."""
    tracker = create_tracker_match(prefix="Live resource summary")
    record_match_change(
        tracker.match_data,
        resources={LiveResource.EVENTS, LiveResource.SHOTS},
        changed_ids={
            LiveResource.EVENTS: {"event-1"},
            LiveResource.SHOTS: {"shot-1"},
        },
    )
    tracker.match_data.refresh_from_db()

    summary = summarize_match_changes(tracker.match_data, since_revision=0)

    assert summary.history_complete is True
    assert summary.resources == {LiveResource.EVENTS, LiveResource.SHOTS}
    assert summary.changed_ids[LiveResource.EVENTS] == {"event-1"}
    assert summary.complete_id_resources == {
        LiveResource.EVENTS,
        LiveResource.SHOTS,
    }

    ahead = summarize_match_changes(
        tracker.match_data,
        since_revision=tracker.match_data.live_revision + 1,
    )
    assert ahead.history_complete is False
    assert ahead.resources == set(LiveResource)


@pytest.mark.django_db(transaction=True)
def test_compact_tracker_poll_reuses_initial_configuration() -> None:
    """Repeated tracker updates omit teams, IDs, and goal type configuration."""
    tracker = create_tracker_match(prefix="Compact tracker")
    initial = get_tracker_state(tracker.match, team=tracker.home_team)

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )
    compact = poll_tracker_state(
        tracker.match,
        team=tracker.home_team,
        since_revision=initial["live_revision"],
        compact=True,
    )
    full = get_tracker_state(tracker.match, team=tracker.home_team)

    assert compact["changed"] is True
    assert compact["resources"] == ["events", "live", "tracker"]
    assert "team" not in compact["patch"]
    assert "opponent" not in compact["patch"]
    assert "goal_types" not in compact["patch"]
    assert len(json.dumps(compact)) < len(json.dumps(full))
