"""Tests for minutes-played computation fallbacks.

These tests protect against a regression where matches without any usable
shot/goal/substitution timestamps would yield a `match_end_minutes` of 1.0.
That, in turn, makes all players appear to have ~0-1 minutes played even when
full match parts were tracked.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from apps.game_tracker.models import GroupType
from apps.game_tracker.services.match_minutes import compute_minutes_by_player_id
from apps.game_tracker.tests.tracker_test_helpers import (
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db
@pytest.mark.parametrize("last_part_minutes", [None, 20, 30, 40])
def test_compute_minutes_prefers_recorded_duration_to_scheduled_fallback(
    last_part_minutes: int | None,
) -> None:
    """Use the schedule only when no recorded period duration is available."""
    tracker = create_tracker_match(
        prefix="Minutes fallback", start_offset=-timedelta(hours=2)
    )
    match_data = tracker.match_data
    match_data.parts = 2
    match_data.part_length = 30 * 60
    match_data.save(update_fields=["parts", "part_length"])
    # The final period can finish early or run longer than scheduled.
    durations = (30, last_part_minutes) if last_part_minutes is not None else ()
    for number, duration in enumerate(durations, start=1):
        start = -100 + (number - 1) * 40
        create_match_part(
            match_data=match_data,
            part_number=number,
            start_offset=timedelta(minutes=start),
            end_offset=timedelta(minutes=start + duration),
            active=False,
        )

    player = create_tracker_player(username="minutes_fallback_player")
    group = create_player_group(
        match_data=match_data,
        team=tracker.home_team,
        group_type=GroupType.objects.create(name="Aanval", order=1),
    )
    group.players.add(player)

    minutes_by_player_id = compute_minutes_by_player_id(match_data=match_data)

    assert str(player.id_uuid) in minutes_by_player_id
    expected_minutes = 60 if last_part_minutes is None else 30 + last_part_minutes
    assert minutes_by_player_id[str(player.id_uuid)] == pytest.approx(
        expected_minutes, abs=0.01
    )
