"""Tests for minutes-played computation fallbacks.

These tests protect against a regression where matches without any usable
shot/goal/substitution timestamps would yield a `match_end_minutes` of 1.0.
That, in turn, makes all players appear to have ~0-1 minutes played even when
full match parts were tracked.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GroupType, MatchData, MatchPart, PlayerGroup
from apps.game_tracker.services.match_minutes import compute_minutes_by_player_id
from apps.player.models.player import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_compute_minutes_uses_match_length_fallback_when_no_timeline_times() -> None:
    """If events/shots have no timestamps, minutes should still be match-length."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - minutes fallback",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(hours=2),
    )

    match_data = MatchData.objects.get(match_link=match)

    # Create two full parts (default: 2 * 1800s = 60 minutes).
    p1_start = timezone.now() - timedelta(minutes=80)
    p1_end = p1_start + timedelta(minutes=30)
    p2_start = p1_end + timedelta(minutes=10)
    p2_end = p2_start + timedelta(minutes=30)

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=p1_start,
        end_time=p1_end,
        active=False,
    )
    MatchPart.objects.create(
        match_data=match_data,
        part_number=2,
        start_time=p2_start,
        end_time=p2_end,
        active=False,
    )

    aanval = GroupType.objects.create(name="Aanval", order=1)

    user = get_user_model().objects.create_user(username="minutes_fallback_player")
    # Project auto-creates Player via signals for new users; fall back if needed.
    player = getattr(user, "player", None) or Player.objects.create(user=user)
    group = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=aanval,
        current_type=aanval,
    )
    group.players.add(player)

    minutes_by_player_id = compute_minutes_by_player_id(match_data=match_data)

    assert str(player.id_uuid) in minutes_by_player_id
    assert minutes_by_player_id[str(player.id_uuid)] == pytest.approx(60.0, abs=0.01)
