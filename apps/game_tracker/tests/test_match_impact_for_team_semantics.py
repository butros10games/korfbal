"""Regression tests for attacker/defender attribution in impact scoring."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

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


@pytest.mark.django_db
def test_v7_scores_for_team_false_as_direct_defensive_responsibility() -> None:
    """A conceded goal must hurt the selected defender, not credit a scorer."""
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season - for_team semantics",
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

    goal_type = GoalType.objects.create(name="Doorloopbal")

    user_scorer = get_user_model().objects.create_user(username="scorer")
    scorer = getattr(user_scorer, "player", None)
    if scorer is None:
        scorer = Player.objects.create(user=user_scorer)

    user_defender = get_user_model().objects.create_user(username="defender")
    defender = getattr(user_defender, "player", None)
    if defender is None:
        defender = Player.objects.create(user=user_defender)

    # A real scored goal for the shooter's team.
    Shot.objects.create(
        player=scorer,
        match_data=match_data,
        match_part=part,
        team=home_team,
        for_team=True,
        scored=True,
        shot_type=goal_type,
        time=part_start + timedelta(minutes=1),
    )

    # A conceded goal tracked against a defending player.
    Shot.objects.create(
        player=defender,
        match_data=match_data,
        match_part=part,
        team=home_team,
        for_team=False,
        scored=True,
        shot_type=goal_type,
        time=part_start + timedelta(minutes=2),
    )

    rows, breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )

    scorer_key = str(scorer.id_uuid)
    defender_key = str(defender.id_uuid)

    rows_by_player = {row.player_id: row for row in rows}
    assert breakdown[scorer_key]["offense_goal_above_expected"]["count"] == 1
    assert rows_by_player[scorer_key].impact_score == Decimal("0.820")
    assert breakdown[defender_key]["defense_goal_below_expected"]["count"] == 1
    assert rows_by_player[defender_key].impact_score == Decimal("-0.820")

    _legacy_rows, legacy_breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version="v6",
    )
    assert legacy_breakdown[defender_key]["goal_scored"]["count"] == 1
