"""Tests for persisted match impact computation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    PlayerMatchImpact,
    PlayerMatchImpactBreakdown,
    Shot,
)
from apps.game_tracker.services.match_impact import (
    compute_match_impact_breakdown,
    compute_match_impact_rows,
    persist_match_impact_rows_with_breakdowns,
    round_js_1dp,
)
from apps.player.models.player import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.04, Decimal("1.0")),
        (1.05, Decimal("1.1")),
        (-1.04, Decimal("-1.0")),
        (-0.15, Decimal("-0.1")),
    ],
)
def test_round_js_1dp_matches_js_math_round(value: float, expected: Decimal) -> None:
    """The Python port should match JS `Math.round(x * 10) / 10` semantics."""
    assert round_js_1dp(value) == expected


@pytest.mark.django_db
def test_compute_match_impact_rows_missed_shot_penalizes_shooter() -> None:
    """A missed shot should penalize the shooter according to the JS logic."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - impact",
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
    part_start = timezone.now() - timedelta(minutes=10)
    part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=part_start,
        active=True,
    )

    user = get_user_model().objects.create_user(username="impact_shooter")
    # Project auto-creates Player via signals for new users; fall back if needed.
    player = getattr(user, "player", None) or Player.objects.create(user=user)

    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=part,
        team=home_team,
        scored=False,
        time=part_start + timedelta(minutes=1),
    )

    rows = compute_match_impact_rows(match_data=match_data)
    by_player = {r.player_id: r for r in rows}

    assert str(player.id_uuid) in by_player
    assert by_player[str(player.id_uuid)].impact_score == Decimal("-0.6")


@pytest.mark.django_db
def test_compute_match_impact_breakdown_includes_missed_shot_category() -> None:
    """Breakdown should explain negative impact from missed shots."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - impact breakdown",
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
    part_start = timezone.now() - timedelta(minutes=10)
    part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=part_start,
        active=True,
    )

    user = get_user_model().objects.create_user(username="impact_breakdown_shooter")
    player = getattr(user, "player", None) or Player.objects.create(user=user)

    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=part,
        team=home_team,
        scored=False,
        time=part_start + timedelta(minutes=1),
    )

    rows, breakdown = compute_match_impact_breakdown(match_data=match_data)
    by_player = {r.player_id: r for r in rows}

    pid = str(player.id_uuid)
    assert pid in by_player
    assert by_player[pid].impact_score == Decimal("-0.6")

    player_breakdown = breakdown.get(pid)
    assert player_breakdown is not None
    assert player_breakdown["shot_miss_for"]["count"] == 1
    assert round_js_1dp(player_breakdown["shot_miss_for"]["points"]) == Decimal("-0.6")

    total_raw = sum(item["points"] for item in player_breakdown.values())
    assert round_js_1dp(total_raw) == Decimal("-0.6")


@pytest.mark.django_db
def test_persist_match_impact_rows_with_breakdowns_creates_db_rows() -> None:
    """Persisting with breakdowns should create PlayerMatchImpactBreakdown rows."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - impact persist breakdown",
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

    user = get_user_model().objects.create_user(username="impact_persist_breakdown")
    player = getattr(user, "player", None) or Player.objects.create(user=user)

    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=part,
        team=home_team,
        scored=False,
        time=part_start + timedelta(minutes=1),
    )

    rows = persist_match_impact_rows_with_breakdowns(match_data=match_data)
    assert rows >= 1

    impact = PlayerMatchImpact.objects.get(match_data=match_data, player=player)
    breakdown = PlayerMatchImpactBreakdown.objects.get(impact=impact)

    assert breakdown.algorithm_version == impact.algorithm_version
    assert "shot_miss_for" in breakdown.breakdown
