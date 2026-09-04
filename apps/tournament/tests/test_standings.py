"""Tournament standings calculation tests."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.tournament.models import (
    Tournament,
    TournamentMatch,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentStandingAdjustment,
    TournamentTeam,
)
from apps.tournament.services.snapshot import build_tournament_snapshot
from apps.tournament.services.standings import calculate_pool_standings


pytestmark = pytest.mark.django_db
EXPECTED_GOAL_DIFFERENCE = 2
EXPECTED_PROVISIONAL_PLAYED = 2
EXPECTED_PROVISIONAL_GOAL_DIFFERENCE = 99


def test_standings_apply_configured_points_goal_difference_and_adjustments() -> None:
    """Official results stay final-only while snapshots can project live scores."""
    user = get_user_model().objects.create_user(username="manager")
    tournament = Tournament.objects.create(
        name="Testtoernooi",
        slug="testtoernooi",
        owner=user,
        starts_at=timezone.now(),
        win_points=3,
        draw_points=1,
        loss_points=0,
    )
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Poules",
        kind=TournamentStage.Kind.POOL,
    )
    pool = TournamentPool.objects.create(
        tournament=tournament,
        stage=stage,
        name="Poule A",
    )
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=name, seed=index)
        for index, name in enumerate(("Blauw", "Groen", "Rood"), start=1)
    ]
    entries = [
        TournamentPoolEntry.objects.create(pool=pool, team=team, seed_order=index)
        for index, team in enumerate(teams, start=1)
    ]
    TournamentStandingAdjustment.objects.create(
        entry=entries[2],
        points=-1,
        reason="Te laat aanwezig",
        created_by=user,
    )
    TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        pool=pool,
        home_team=teams[0],
        away_team=teams[1],
        match_number=1,
        status=TournamentMatch.Status.FINAL,
        home_score=8,
        away_score=6,
    )
    TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        pool=pool,
        home_team=teams[1],
        away_team=teams[2],
        match_number=2,
        status=TournamentMatch.Status.FINAL,
        home_score=5,
        away_score=5,
    )
    TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        pool=pool,
        home_team=teams[2],
        away_team=teams[0],
        match_number=3,
        status=TournamentMatch.Status.LIVE,
        home_score=99,
        away_score=0,
    )

    standings = calculate_pool_standings(pool)

    assert [row["team_name"] for row in standings] == ["Blauw", "Groen", "Rood"]
    assert standings[0]["points"] == tournament.win_points
    assert standings[0]["goal_difference"] == EXPECTED_GOAL_DIFFERENCE
    assert standings[1]["points"] == 1
    assert standings[2]["points"] == 0
    assert standings[2]["adjustment"] == -1

    provisional = calculate_pool_standings(pool, include_live_matches=True)

    assert [row["team_name"] for row in provisional] == ["Rood", "Blauw", "Groen"]
    assert provisional[0]["played"] == EXPECTED_PROVISIONAL_PLAYED
    assert provisional[0]["points"] == tournament.win_points
    assert provisional[0]["goal_difference"] == EXPECTED_PROVISIONAL_GOAL_DIFFERENCE

    snapshot = build_tournament_snapshot(tournament)

    assert snapshot["pools"][0]["standings"][0]["team_name"] == "Rood"
