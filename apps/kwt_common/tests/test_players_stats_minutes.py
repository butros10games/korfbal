"""Tests for team/season player stats minutes-played aggregation.

Minutes-played is read from persisted `PlayerMatchMinutes` rows.
When minutes data is missing for a specific player (no persisted row), the API
should return `null` (not `0.0`) to avoid implying the player played 0 minutes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.game_tracker.models import MatchData, PlayerMatchMinutes, Shot
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.game_tracker.tests.tracker_test_helpers import (
    create_match_part,
    create_tracker_match,
    create_tracker_player,
)
from apps.kwt_common.utils.players_stats import build_player_stats


@pytest.mark.django_db
def test_build_player_stats_minutes_missing_returns_null(
    settings: SettingsWrapper,
) -> None:
    """If minutes are missing (no persisted row), the API returns null, not 0.0."""
    settings.KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE = False

    tracker = create_tracker_match(
        prefix="Missing minutes", start_offset=-timedelta(minutes=30)
    )
    match_data = tracker.match_data
    home_team = tracker.home_team
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    part = create_match_part(
        match_data=match_data, start_offset=-timedelta(minutes=10), active=True
    )
    part_start = part.start_time

    player_a = create_tracker_player(username="minutes_a")

    player_b = create_tracker_player(username="minutes_b")

    # Ensure both players show up in the stat rows (they must have at least one shot).
    Shot.objects.create(
        player=player_a,
        match_data=match_data,
        match_part=part,
        team=home_team,
        for_team=True,
        scored=False,
        time=part_start + timedelta(minutes=1),
    )
    Shot.objects.create(
        player=player_b,
        match_data=match_data,
        match_part=part,
        team=home_team,
        for_team=True,
        scored=False,
        time=part_start + timedelta(minutes=2),
    )

    # Persist minutes for only one player.
    PlayerMatchMinutes.objects.update_or_create(
        match_data=match_data,
        player=player_a,
        algorithm_version=LATEST_MATCH_MINUTES_VERSION,
        defaults={"minutes_played": Decimal("10.00")},
    )

    rows = async_to_sync(build_player_stats)(
        [player_a, player_b],
        MatchData.objects.filter(id_uuid=match_data.id_uuid),
    )

    expected_rows = 2
    minutes_a = 10.0

    assert len(rows) == expected_rows

    minutes_by_username = {row["username"]: row["minutes_played"] for row in rows}
    assert minutes_by_username["minutes_a"] == minutes_a
    assert minutes_by_username["minutes_b"] is None
