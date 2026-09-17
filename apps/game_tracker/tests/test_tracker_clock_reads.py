"""Clock snapshots preserve pause semantics without hydrating pause history."""

from datetime import timedelta
from http import HTTPStatus
from typing import Any
from unittest.mock import Mock, patch

from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.models import MatchData, Pause
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_match_part,
    create_tracker_match,
)


pytestmark = pytest.mark.django_db
CLOCK_SELECTS = 1
CLOCK_HISTORY_COUNT = 50
COMPLETED_PAUSE_SECONDS = 3.875003


def _read_clock(
    client: Client, tracker: TrackerMatchContext, surface: str
) -> dict[str, Any]:
    if surface == "tracker":
        return get_tracker_state(tracker.match, team=tracker.home_team)
    response = client.get(f"/api/matches/{tracker.match.pk}/{surface}/")
    assert response.status_code == HTTPStatus.OK
    return response.json()


@pytest.mark.parametrize("surface", ["live", "tracker"])
@pytest.mark.parametrize(
    ("status", "active_pause"),
    [
        ("active", False),
        ("active", True),
        ("finished", False),
        ("finished", True),
        ("upcoming", False),
    ],
)
def test_clock_preserves_fractional_completed_and_active_pauses(
    client: Client, surface: str, status: str, active_pause: bool
) -> None:
    """Ended/incomplete pauses and non-active statuses retain their wire meaning."""
    tracker = create_tracker_match(prefix="Clock contract")
    part = create_match_part(match_data=tracker.match_data)
    pauses = [
        Pause(
            match_data=tracker.match_data,
            match_part=part,
            start_time=part.start_time + timedelta(seconds=index),
            end_time=part.start_time + timedelta(seconds=index + seconds),
        )
        for index, seconds in enumerate((1.25, 2.5, 0.125, 0.000003, 0))
    ]
    pauses.extend([
        Pause(match_data=tracker.match_data, match_part=part),
        Pause(
            match_data=tracker.match_data, match_part=part, start_time=part.start_time
        ),
    ])
    Pause.objects.bulk_create(pauses)
    active_start = part.start_time + timedelta(seconds=30)
    if active_pause:
        Pause.objects.create(
            match_data=tracker.match_data,
            match_part=part,
            active=True,
            start_time=active_start,
        )
    MatchData.objects.filter(pk=tracker.match_data.pk).update(status=status)
    state = _read_clock(client, tracker, surface)
    assert state["paused"] is (status != "active" or active_pause)
    timer = state["timer"]
    assert timer.pop("server_time")
    expected = {
        "match_data_id": str(tracker.match_data.pk),
        "time": part.start_time.isoformat(),
        "length": tracker.match_data.part_length,
        "pause_length": pytest.approx(COMPLETED_PAUSE_SECONDS, abs=1e-9),
        "type": "pause" if active_pause else "active",
    }
    if active_pause:
        expected["calc_to"] = active_start.isoformat()
    assert timer == expected


@pytest.mark.parametrize("surface", ["live", "tracker"])
def test_clock_ignores_pauses_outside_the_current_part(
    client: Client, surface: str
) -> None:
    """A previous-period pause cannot freeze or shorten the current clock."""
    tracker = create_tracker_match(prefix="Clock scope")
    old_part = create_match_part(match_data=tracker.match_data, active=False)
    part = create_match_part(match_data=tracker.match_data, part_number=2)
    other = create_tracker_match(prefix="Other clock")
    for match_data, match_part in (
        (tracker.match_data, old_part),
        (tracker.match_data, None),
        (other.match_data, part),
    ):
        Pause.objects.create(
            match_data=match_data,
            match_part=match_part,
            start_time=part.start_time,
            end_time=part.start_time + timedelta(seconds=300),
        )
    Pause.objects.create(
        match_data=tracker.match_data,
        match_part=old_part,
        active=True,
        start_time=part.start_time,
    )
    MatchData.objects.filter(pk=tracker.match_data.pk).update(status="active")
    state = _read_clock(client, tracker, surface)
    assert state["paused"] is False
    assert state["timer"]["type"] == "active"
    assert state["timer"]["pause_length"] == 0
    assert state["timer"]["time"] == part.start_time.isoformat()


@pytest.mark.parametrize("surface", ["live", "tracker"])
@pytest.mark.parametrize("status", ["upcoming", "active", "finished"])
def test_clock_without_active_part_is_deactivated(
    client: Client, surface: str, status: str
) -> None:
    """Missing or completed periods keep tracker actions paused."""
    tracker = create_tracker_match(prefix="No active clock")
    create_match_part(match_data=tracker.match_data, active=False)
    MatchData.objects.filter(pk=tracker.match_data.pk).update(status=status)
    state = _read_clock(client, tracker, surface)
    assert state["paused"] is True
    assert state["timer"] == {
        "type": "deactivated",
        "match_data_id": str(tracker.match_data.pk),
    }


@pytest.mark.parametrize("surface", ["live", "live/poll"])
def test_public_clock_uses_one_pause_query_without_model_hydration(
    client: Client, surface: str
) -> None:
    """Clock reads aggregate history instead of creating Pause model instances."""
    tracker = create_tracker_match(prefix="Clock query bound")
    part = create_match_part(match_data=tracker.match_data)
    Pause.objects.bulk_create([
        Pause(
            match_data=tracker.match_data,
            match_part=part,
            start_time=part.start_time + timedelta(seconds=index * 2),
            end_time=part.start_time + timedelta(seconds=index * 2 + 1),
        )
        for index in range(CLOCK_HISTORY_COUNT)
    ])
    MatchData.objects.filter(pk=tracker.match_data.pk).update(status="active")
    load_pause = Mock(wraps=Pause.from_db.__func__)
    with (
        patch.object(Pause, "from_db", classmethod(load_pause)),
        CaptureQueriesContext(connection) as queries,
    ):
        state = _read_clock(client, tracker, surface)
    assert state["timer"]["pause_length"] == CLOCK_HISTORY_COUNT
    pause_queries = [q for q in queries if 'FROM "game_tracker_pause"' in q["sql"]]
    assert len(pause_queries) == CLOCK_SELECTS
    load_pause.assert_not_called()


def test_tracker_pause_flag_and_timer_observe_the_same_pause() -> None:
    """A pause during roster preparation cannot disagree with its control flag."""
    tracker = create_tracker_match(prefix="Clock read race")
    part = create_match_part(match_data=tracker.match_data)
    MatchData.objects.filter(pk=tracker.match_data.pk).update(status="active")

    def pause_during_roster(*args: object, **kwargs: object) -> list[object]:
        del args, kwargs
        Pause.objects.create(
            match_data=tracker.match_data,
            match_part=part,
            start_time=part.start_time,
            active=True,
        )
        return []

    with patch(
        "apps.game_tracker.services.tracker_state._player_groups_payload",
        side_effect=pause_during_roster,
    ):
        state = get_tracker_state(tracker.match, team=tracker.home_team)
    assert state["timer"]["type"] == "pause"
    assert state["paused"] is True
    assert state["start_stop_label"] == "Start"
