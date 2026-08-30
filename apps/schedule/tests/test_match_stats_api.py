"""HTTP contracts for match statistics and player-side resolution."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal

from django.contrib.auth.models import User
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    GoalType,
    GroupType,
    MatchData,
    MatchPlayer,
    PlayerGroup,
    Shot,
)
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


Side = Literal["home", "away"]


@dataclass(frozen=True)
class MatchContext:
    """The small graph shared by match-stats HTTP tests."""

    match: Match
    match_data: MatchData
    season: Season
    home_team: Team
    away_team: Team
    goal_type: GoalType


@dataclass(frozen=True)
class SideScenario:
    """Conflicting player-side evidence and its expected resolution."""

    name: str
    expected_side: Side
    roster: tuple[Side, ...] = ()
    groups: tuple[Side, ...] = ()
    team_data: tuple[Side, ...] = ()
    shots: tuple[Side, ...] = ()


SIDE_RESOLUTION_SCENARIOS = (
    SideScenario(
        name="roster_over_shots",
        expected_side="home",
        roster=("home",),
        shots=("away",),
    ),
    SideScenario(
        name="playergroup_over_shots",
        expected_side="home",
        groups=("home",),
        shots=("home", "home", "away", "away", "away", "away"),
    ),
    SideScenario(
        name="teamdata_over_shots",
        expected_side="home",
        team_data=("home",),
        shots=("away",),
    ),
    SideScenario(
        name="playergroup_over_teamdata",
        expected_side="home",
        groups=("home",),
        team_data=("away",),
        shots=("away",),
    ),
    SideScenario(
        name="ambiguous_groups_fall_back_to_teamdata",
        expected_side="home",
        groups=("home", "away"),
        team_data=("home",),
        shots=("home", "away"),
    ),
    SideScenario(
        name="ambiguous_assignments_fall_back_to_shot_side",
        expected_side="away",
        groups=("home", "away"),
        team_data=("home", "away"),
        shots=("away", "away"),
    ),
    SideScenario(
        name="mixed_shots_use_majority",
        expected_side="away",
        shots=("home", "home", "away", "away", "away"),
    ),
    SideScenario(
        name="equal_shots_tie_break_home",
        expected_side="home",
        shots=("home", "home", "away", "away"),
    ),
)


def _create_match_context() -> MatchContext:
    today = timezone.localdate()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)
    home_team = Team.objects.create(
        name="Home Team",
        club=Club.objects.create(name="Home Club"),
    )
    away_team = Team.objects.create(
        name="Away Team",
        club=Club.objects.create(name="Away Club"),
    )
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )
    return MatchContext(
        match=match,
        match_data=MatchData.objects.get(match_link=match),
        season=season,
        home_team=home_team,
        away_team=away_team,
        goal_type=GoalType.objects.create(name="Doorloop"),
    )


def _team(context: MatchContext, side: Side) -> Team:
    return context.home_team if side == "home" else context.away_team


def _create_player(username: str) -> Player:
    user = User.objects.create_user(username=username)
    return Player.objects.get(user=user)


def _add_shot(
    context: MatchContext,
    player: Player,
    side: Side,
    *,
    scored: bool = False,
    goal_type: GoalType | None = None,
) -> None:
    Shot.objects.create(
        match_data=context.match_data,
        team=_team(context, side),
        player=player,
        scored=scored,
        shot_type=goal_type or context.goal_type,
    )


def _apply_side_evidence(
    context: MatchContext,
    player: Player,
    scenario: SideScenario,
) -> None:
    for side in scenario.roster:
        MatchPlayer.objects.create(
            match_data=context.match_data,
            team=_team(context, side),
            player=player,
        )

    if scenario.groups:
        group_type = GroupType.objects.create(name="Reserve", order=0)
        for side in scenario.groups:
            group = PlayerGroup.objects.create(
                match_data=context.match_data,
                team=_team(context, side),
                starting_type=group_type,
                current_type=group_type,
            )
            group.players.add(player)

    for side in scenario.team_data:
        team_data = TeamData.objects.create(
            team=_team(context, side),
            season=context.season,
        )
        team_data.players.add(player)

    for side in scenario.shots:
        _add_shot(context, player, side)


@pytest.mark.django_db
def test_match_stats_returns_home_vs_away_aggregates(client: Client) -> None:
    """The endpoint reports match, goal-type, and per-player aggregates."""
    context = _create_match_context()
    home_player = _create_player("home_player")
    away_player = _create_player("away_player")
    bench_player = _create_player("bench_player")

    MatchPlayer.objects.create(
        match_data=context.match_data,
        team=context.home_team,
        player=bench_player,
    )
    free_ball = GoalType.objects.create(name="Vrijebal")
    _add_shot(context, home_player, "home", scored=True)
    _add_shot(context, home_player, "home", scored=True)
    _add_shot(context, home_player, "home")
    _add_shot(context, away_player, "away", scored=True)
    _add_shot(
        context,
        away_player,
        "away",
        scored=True,
        goal_type=free_ball,
    )

    response = client.get(f"/api/matches/{context.match.id_uuid}/stats/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert {key: payload["meta"][key] for key in ("home_team_id", "away_team_id")} == {
        "home_team_id": str(context.home_team.id_uuid),
        "away_team_id": str(context.away_team.id_uuid),
    }
    general = payload["general"]
    assert {
        key: general[key]
        for key in ("shots_for", "shots_against", "goals_for", "goals_against")
    } == {
        "shots_for": 3,
        "shots_against": 2,
        "goals_for": 2,
        "goals_against": 2,
    }
    assert general["team_goal_stats"] == {
        "Doorloop": {"goals_by_player": 2, "goals_against_player": 1},
        "Vrijebal": {"goals_by_player": 0, "goals_against_player": 1},
    }
    assert {"Doorloop", "Vrijebal"}.issubset({
        entry["name"] for entry in general["goal_types"]
    })

    players = payload["players"]
    assert {line["username"] for line in players["home"]} == {
        "bench_player",
        "home_player",
    }
    assert {line["username"] for line in players["away"]} == {"away_player"}
    lines = {
        line["username"]: line for side in ("home", "away") for line in players[side]
    }
    stat_keys = ("shots_for", "shots_against", "goals_for", "goals_against")
    assert {key: lines["home_player"][key] for key in stat_keys} == {
        "shots_for": 3,
        "shots_against": 0,
        "goals_for": 2,
        "goals_against": 0,
    }
    assert {key: lines["away_player"][key] for key in stat_keys} == {
        "shots_for": 2,
        "shots_against": 0,
        "goals_for": 2,
        "goals_against": 0,
    }
    assert {key: lines["bench_player"][key] for key in stat_keys} == dict.fromkeys(
        stat_keys,
        0,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scenario",
    SIDE_RESOLUTION_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_match_stats_resolves_player_side(
    client: Client,
    scenario: SideScenario,
) -> None:
    """Conflicting sources resolve through roster, group, team, and shot evidence."""
    context = _create_match_context()
    player = _create_player(scenario.name)
    _apply_side_evidence(context, player, scenario)

    response = client.get(f"/api/matches/{context.match.id_uuid}/stats/")

    assert response.status_code == HTTPStatus.OK
    players = response.json()["players"]
    expected_usernames = {
        "home": {scenario.name} if scenario.expected_side == "home" else set(),
        "away": {scenario.name} if scenario.expected_side == "away" else set(),
    }
    assert {
        side: {line["username"] for line in players[side]} for side in ("home", "away")
    } == expected_usernames
