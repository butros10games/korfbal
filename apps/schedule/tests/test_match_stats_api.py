"""HTTP contracts for match statistics and player-side resolution."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal

from django.test.client import Client
from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    GoalType,
    GroupType,
    MatchData,
    MatchPlayer,
    PlayerGroup,
    PossessionChange,
    Shot,
)
from apps.game_tracker.services.match_stats_payload import build_match_stats_payload
from apps.game_tracker.tests.tracker_test_helpers import (
    create_match_part,
    create_tracker_match,
    create_tracker_player,
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
    tracker = create_tracker_match(prefix="Match stats")
    assert tracker.match.season is not None
    return MatchContext(
        match=tracker.match,
        match_data=tracker.match_data,
        season=tracker.match.season,
        home_team=tracker.home_team,
        away_team=tracker.away_team,
        goal_type=GoalType.objects.create(name="Doorloop"),
    )


def _team(context: MatchContext, side: Side) -> Team:
    return context.home_team if side == "home" else context.away_team


def _create_player(username: str) -> Player:
    return create_tracker_player(username=username)


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


@pytest.mark.django_db
@pytest.mark.parametrize("extra_types", [0, 8])
@pytest.mark.parametrize("populated", [False, True])
def test_match_stats_query_budget_does_not_grow_with_goal_type_count(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
    extra_types: int,
    populated: bool,
) -> None:
    """Count only this match's events, including untyped and unattributed records."""
    context = _create_match_context()
    goal_types = [context.goal_type] + [
        GoalType.objects.create(name=f"Unused {index}") for index in range(extra_types)
    ]
    player = _create_player("aggregate_player")
    if populated:
        _add_shot(context, player, "home", scored=True)
        _add_shot(context, player, "home")
        _add_shot(context, player, "away", scored=True)
        Shot.objects.create(
            match_data=context.match_data,
            team=context.home_team,
            player=player,
            scored=True,
        )
        # A shot without a team contributes to neither side.
        Shot.objects.create(match_data=context.match_data, player=player, scored=True)
        part = create_match_part(match_data=context.match_data)
        for side, kinds in (
            ("home", ("ball_loss", "ball_loss", "interception")),
            ("away", ("ball_loss", "interception", "interception")),
        ):
            for kind in kinds:
                PossessionChange.objects.create(
                    match_data=context.match_data,
                    match_part=part,
                    team=_team(context, side),
                    kind=kind,
                    time=timezone.now(),
                )

    other_match = Match.objects.create(
        home_team=context.home_team,
        away_team=context.away_team,
        season=context.season,
        start_time=timezone.now(),
    )
    other_data = MatchData.objects.get(match_link=other_match)
    Shot.objects.create(
        match_data=other_data,
        team=context.home_team,
        player=player,
        scored=True,
        shot_type=context.goal_type,
    )
    PossessionChange.objects.create(
        match_data=other_data,
        match_part=create_match_part(match_data=other_data),
        team=context.home_team,
        kind="ball_loss",
        time=timezone.now(),
    )
    with django_assert_num_queries(14 if populated else 7):
        general = build_match_stats_payload(
            match=context.match, match_data=context.match_data
        )["general"]
    assert general == {
        "shots_for": 3 if populated else 0,
        "shots_against": 1 if populated else 0,
        "goals_for": 2 if populated else 0,
        "goals_against": 1 if populated else 0,
        "ball_losses_for": 2 if populated else 0,
        "ball_losses_against": 1 if populated else 0,
        "interceptions_for": 1 if populated else 0,
        "interceptions_against": 2 if populated else 0,
        "team_goal_stats": {
            goal_type.name: {
                "goals_by_player": int(populated and goal_type == context.goal_type),
                "goals_against_player": int(
                    populated and goal_type == context.goal_type
                ),
            }
            for goal_type in goal_types
        },
        "goal_types": [
            {"id": str(goal_type.pk), "name": goal_type.name}
            for goal_type in goal_types
        ],
    }
