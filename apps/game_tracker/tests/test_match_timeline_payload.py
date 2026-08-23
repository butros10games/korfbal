"""Regression tests for tracker timeline clock calculations."""

from datetime import timedelta

import pytest

from apps.game_tracker.models import Pause, PlayerChange
from apps.game_tracker.services.match_timeline_payload import (
    serialize_substitute_event,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db
def test_substitution_during_active_pause_uses_frozen_match_clock() -> None:
    """An open pause contributes elapsed pause time up to the event timestamp."""
    tracker = create_tracker_match(prefix="Paused timeline")
    part = create_match_part(
        match_data=tracker.match_data,
        start_offset=-timedelta(minutes=7),
    )
    pause = Pause.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        start_time=part.start_time + timedelta(minutes=5),
        active=True,
    )
    group_type = create_group_types("Paused timeline attack")["Paused timeline attack"]
    player_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_type,
    )
    change = PlayerChange.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        player_group=player_group,
        player_in=create_tracker_player(username="paused-timeline-in"),
        player_out=create_tracker_player(username="paused-timeline-out"),
        time=pause.start_time + timedelta(minutes=2),
    )

    payload = serialize_substitute_event(tracker.match_data, change)

    assert payload is not None
    assert payload["time"] == "5"
