"""Regression coverage for durable live-match revisions."""

from __future__ import annotations

import pytest

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.tracker_http import apply_tracker_command
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
