"""Tests for goal streak bonus capping.

The match impact algorithm applies a *team* goal streak multiplier.
To keep goal impact totals intuitive, we cap the maximum streak bonus.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, MatchPart, Shot
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    compute_match_impact_breakdown,
)
from apps.player.models.player import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


GOAL_COUNT = 6


@pytest.mark.django_db
def test_goal_points_streak_bonus_is_capped() -> None:
    """Long scoring streaks should not increase goal points without bound."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - streak cap",
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

    # "doorloop" => type weight 1.25.
    goal_type = GoalType.objects.create(name="Doorloopbal")

    user = get_user_model().objects.create_user(username="streak_scorer")
    scorer = getattr(user, "player", None)
    if scorer is None:
        scorer = Player.objects.create(user=user)

    # Create consecutive goals by the same team and player.
    for i in range(GOAL_COUNT):
        Shot.objects.create(
            player=scorer,
            match_data=match_data,
            match_part=part,
            team=home_team,
            for_team=True,
            scored=True,
            shot_type=goal_type,
            time=part_start + timedelta(minutes=i + 1),
        )

    _rows, breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )

    per_player = breakdown[str(scorer.id_uuid)]
    assert per_player["goal_scored"]["count"] == GOAL_COUNT

    # Expected with capped streak (max streak=4):
    # base = 3.2 * 1.25 = 4.0
    # streak factors: 1.00, 1.12, 1.24, 1.36, 1.36, 1.36
    expected_total = 4.0 * (1.00 + 1.12 + 1.24 + 1.36 + 1.36 + 1.36)

    assert per_player["goal_scored"]["points"] == pytest.approx(expected_total)
