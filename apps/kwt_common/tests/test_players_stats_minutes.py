"""Tests for team/season player stats minutes-played aggregation.

Minutes-played is read from persisted `PlayerMatchMinutes` rows.
When minutes data is missing for a specific player (no persisted row), the API
should return `null` (not `0.0`) to avoid implying the player played 0 minutes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.club.models import Club
from apps.game_tracker.models import MatchData, MatchPart, PlayerMatchMinutes, Shot
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.kwt_common.utils.players_stats import build_player_stats
from apps.player.models.player import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_build_player_stats_minutes_missing_returns_null(
    settings: SettingsWrapper,
) -> None:
    """If minutes are missing (no persisted row), the API returns null, not 0.0."""
    settings.KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE = False

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - players_stats minutes",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    part_start = timezone.now() - timedelta(minutes=10)
    part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=part_start,
        active=True,
    )

    user_a = get_user_model().objects.create_user(username="minutes_a")
    player_a = getattr(user_a, "player", None) or Player.objects.create(user=user_a)

    user_b = get_user_model().objects.create_user(username="minutes_b")
    player_b = getattr(user_b, "player", None) or Player.objects.create(user=user_b)

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
