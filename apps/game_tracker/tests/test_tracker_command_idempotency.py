"""Regression coverage for tracker command idempotency and ordering."""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.game_tracker.models import MatchPart, Pause, TrackerCommand
from apps.game_tracker.services.tracker_http import (
    TrackerCommandError,
    apply_tracker_command,
)
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


@pytest.mark.django_db
def test_retried_command_is_applied_once() -> None:
    """A repeated command id returns state without replaying the transition."""
    tracker = create_tracker_match(prefix="Idempotent start")
    command_id = str(uuid4())
    payload = {
        "command": "start/pause",
        "command_id": command_id,
        "expected_revision": 0,
    }

    first = apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload=payload,
    )
    replay = apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={**payload, "client_time_ms": 123},
    )

    assert first["status"] == "active"
    assert replay["status"] == "active"
    assert replay["paused"] is False
    assert MatchPart.objects.filter(match_data=tracker.match_data).count() == 1
    assert Pause.objects.filter(match_data=tracker.match_data).count() == 0
    receipt = TrackerCommand.objects.get(command_id=command_id)
    assert receipt.sequence == 1
    assert replay["command_sequence"] == 1
    assert replay["live_revision"] == 1


@pytest.mark.django_db
def test_reused_command_id_with_different_payload_is_rejected() -> None:
    """An idempotency key cannot silently identify two different commands."""
    tracker = create_tracker_match(prefix="Conflicting command")
    command_id = str(uuid4())
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause", "command_id": command_id},
    )

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "part_end", "command_id": command_id},
        )

    assert error.value.code == "idempotency_conflict"
    assert TrackerCommand.objects.filter(match_data=tracker.match_data).count() == 1


@pytest.mark.django_db
def test_stale_expected_revision_is_rejected_without_consuming_sequence() -> None:
    """A compare-and-set conflict must not create a receipt or change state."""
    tracker = create_tracker_match(prefix="Stale command")
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause", "command_id": str(uuid4())},
    )

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "start/pause",
                "command_id": str(uuid4()),
                "expected_revision": 0,
            },
        )

    assert error.value.code == "revision_conflict"
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == 1
    assert TrackerCommand.objects.filter(match_data=tracker.match_data).count() == 1
    assert Pause.objects.filter(match_data=tracker.match_data).count() == 0


@pytest.mark.django_db
def test_committed_commands_receive_monotonic_sequences() -> None:
    """Sequence records commit order independently from effective timestamps."""
    tracker = create_tracker_match(prefix="Command order")
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause", "command_id": str(uuid4())},
    )
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause", "command_id": str(uuid4())},
    )

    assert list(
        TrackerCommand.objects
        .filter(match_data=tracker.match_data)
        .order_by("sequence")
        .values_list("sequence", flat=True)
    ) == [1, 2]
