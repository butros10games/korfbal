"""Regression coverage for durable live-match revisions."""

from __future__ import annotations

import json
import tracemalloc

import pytest

from apps.game_tracker.composition import apply_tracker_command, record_match_change
from apps.game_tracker.models import MatchData, MatchLiveChange
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.live_updates import (
    MatchChangeSummary,
    summarize_match_changes,
)
from apps.game_tracker.services.tracker_state import (
    get_tracker_state,
    poll_tracker_state,
)
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


UNDO_REVISION = 3
STALE_WRITERS_REVISION = 2
LARGE_REVISION = 1_000_000
RETAINED_REVISIONS = 512
RECONNECT_MEMORY_LIMIT_BYTES = 8 * 1024 * 1024


@pytest.mark.django_db
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


@pytest.mark.django_db
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
    assert state["last_event"]["type"] == "match_part"
    assert state["last_event"]["transition"] == "start"
    assert state["last_event"]["part_number"] == 1


@pytest.mark.django_db
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


@pytest.mark.django_db
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


@pytest.mark.django_db
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


@pytest.mark.django_db
@pytest.mark.parametrize("since_revision", [-1, 0])
def test_reconnect_summary_memory_is_bounded_by_retained_history(
    since_revision: int,
) -> None:
    """An old cursor must not allocate every revision that has been discarded."""
    tracker = create_tracker_match(prefix="Bounded reconnect")
    tracker.match_data.live_revision = LARGE_REVISION
    MatchLiveChange.objects.bulk_create([
        MatchLiveChange(
            match_data=tracker.match_data,
            revision=revision,
            resources=["events"],
            changed_ids={"events": [f"event-{revision}"]},
        )
        for revision in range(
            LARGE_REVISION - RETAINED_REVISIONS + 1, LARGE_REVISION + 1
        )
    ])

    tracemalloc.start()
    try:
        summary = summarize_match_changes(
            tracker.match_data, since_revision=since_revision
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert summary == MatchChangeSummary(
        frozenset(LiveResource), {}, frozenset(), False
    )
    assert peak_bytes < RECONNECT_MEMORY_LIMIT_BYTES


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("current", "cursor", "revisions", "complete"),
    [
        (0, -1, [], True),
        (0, 0, [], True),
        (0, 1, [], False),
        (3, -1, [1, 2, 3], True),
        (3, 0, [1, 2, 3], True),
        (3, 1, [1, 2, 3, 4], True),
        (3, 0, [2, 3], False),
        (3, 0, [1, 3], False),
        (3, 0, [1, 2], False),
        (3, -1, [0, 2, 3], False),
        (3, 0, [], False),
        (3, 3, [1, 3], True),
        (3, 4, [1, 2, 3], False),
        (
            LARGE_REVISION,
            LARGE_REVISION - 2,
            [LARGE_REVISION - 1, LARGE_REVISION],
            True,
        ),
    ],
)
def test_change_summary_retains_revision_window_contract(
    current: int, cursor: int, revisions: list[int], complete: bool
) -> None:
    """Initial, gapped, caught-up and future cursors keep their recovery behavior."""
    tracker = create_tracker_match(prefix="Revision windows")
    tracker.match_data.live_revision = current
    MatchLiveChange.objects.bulk_create([
        MatchLiveChange(
            match_data=tracker.match_data,
            revision=revision,
            resources=["events"],
            changed_ids={"events": [f"event-{revision}"]},
        )
        for revision in revisions
    ])
    summary = summarize_match_changes(tracker.match_data, since_revision=cursor)
    if not complete:
        expected = MatchChangeSummary(frozenset(LiveResource), {}, frozenset(), False)
    else:
        ids = {
            f"event-{revision}"
            for revision in revisions
            if cursor < revision <= current
        }
        expected = (
            MatchChangeSummary(
                frozenset({LiveResource.EVENTS}),
                {LiveResource.EVENTS: frozenset(ids)},
                frozenset({LiveResource.EVENTS}),
                True,
            )
            if ids
            else MatchChangeSummary(frozenset(), {}, frozenset(), True)
        )
    assert summary == expected
