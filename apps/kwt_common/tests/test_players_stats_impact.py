"""Tests for team/season player stats impact aggregation.

These tests ensure that the Team page "impact" totals are consistent with the
Match page algorithm by recomputing persisted per-match impact rows when needed.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, MatchPart, PlayerMatchImpact, Shot
from apps.kwt_common.utils.players_stats import build_player_stats
from apps.player.models.player import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


EXPECTED_SINGLE_MISS_IMPACT = -0.6
EXPECTED_EFFICIENCY_V3_MISSES_IMPACT = -3.4


@pytest.mark.django_db
def test_build_player_stats_recomputes_outdated_match_impacts() -> None:
    """If impacts exist only at an older algorithm version, recompute to latest."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - players_stats",
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

    user = get_user_model().objects.create_user(username="impact_recompute")
    player = getattr(user, "player", None) or Player.objects.create(user=user)

    # A missed shot should yield -0.6 impact in the match-page algorithm.
    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=part,
        team=home_team,
        scored=False,
        time=part_start + timedelta(minutes=1),
    )

    # Seed an outdated persisted impact row that must be replaced.
    PlayerMatchImpact.objects.update_or_create(
        match_data=match_data,
        player=player,
        defaults={
            "team": home_team,
            "impact_score": Decimal("5.0"),
            "algorithm_version": "v0",
        },
    )

    # build_player_stats should recompute the match impacts to latest and return them.
    rows = async_to_sync(build_player_stats)(
        [player],
        MatchData.objects.filter(id_uuid=match_data.id_uuid),
    )

    assert len(rows) == 1
    assert rows[0]["username"] == "impact_recompute"
    assert rows[0]["impact_score"] == EXPECTED_SINGLE_MISS_IMPACT
    assert rows[0]["impact_is_stored"] is True

    updated = PlayerMatchImpact.objects.get(match_data=match_data, player=player)
    assert updated.algorithm_version == "v5"
    assert float(updated.impact_score) == EXPECTED_SINGLE_MISS_IMPACT


@pytest.mark.django_db
def test_build_player_stats_applies_v3_efficiency_scaling_to_misses() -> None:
    """With >=5 shots, v3 scales miss penalty based on shooting efficiency."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - players_stats v3",
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

    user = get_user_model().objects.create_user(username="impact_eff_v3")
    player = getattr(user, "player", None) or Player.objects.create(user=user)

    # 5 misses => 0/5 = 0% efficiency, so v3 applies a harsher miss multiplier (1.15).
    # Each miss: -0.6 * 1.15 = -0.69, total -3.45 which JS-rounds to -3.4.
    for i in range(5):
        Shot.objects.create(
            player=player,
            match_data=match_data,
            match_part=part,
            team=home_team,
            scored=False,
            time=part_start + timedelta(minutes=i + 1),
        )

    # Force recomputation.
    PlayerMatchImpact.objects.update_or_create(
        match_data=match_data,
        player=player,
        defaults={
            "team": home_team,
            "impact_score": Decimal("0.0"),
            "algorithm_version": "v0",
        },
    )

    rows = async_to_sync(build_player_stats)(
        [player],
        MatchData.objects.filter(id_uuid=match_data.id_uuid),
    )

    assert len(rows) == 1
    assert rows[0]["username"] == "impact_eff_v3"
    assert rows[0]["impact_score"] == EXPECTED_EFFICIENCY_V3_MISSES_IMPACT
    assert rows[0]["impact_is_stored"] is True

    updated = PlayerMatchImpact.objects.get(match_data=match_data, player=player)
    assert updated.algorithm_version == "v5"
    assert float(updated.impact_score) == EXPECTED_EFFICIENCY_V3_MISSES_IMPACT
