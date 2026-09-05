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
def test_compute_minutes_uses_match_length_fallback_when_no_timeline_times() -> None:
    """If events/shots have no timestamps, minutes should still be match-length."""
    tracker = create_tracker_match(
        prefix="Minutes fallback", start_offset=-timedelta(hours=2)
    )
    match_data = tracker.match_data
    # Two 30-minute parts with a 10-minute intermission.
    for number, start in enumerate((-80, -40), start=1):
        create_match_part(
            match_data=match_data,
            part_number=number,
            start_offset=timedelta(minutes=start),
            end_offset=timedelta(minutes=start + 30),
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
    assert minutes_by_player_id[str(player.id_uuid)] == pytest.approx(60.0, abs=0.01)
