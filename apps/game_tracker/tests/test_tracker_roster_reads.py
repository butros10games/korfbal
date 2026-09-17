"""Tracker rosters retain names, scope and roles with bounded shared reads."""

from datetime import timedelta
from typing import Any
from unittest.mock import Mock, patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.game_tracker.models import GoalType, GroupType, MatchData, PlayerGroup, Shot
from apps.game_tracker.services import tracker_state
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import Player
from apps.team.models import Team


pytestmark = pytest.mark.django_db
ROSTER_SELECTS = 2


def _group(
    tracker: TrackerMatchContext,
    team: Team,
    starting: str,
    current: str,
    players: list[Player],
) -> PlayerGroup:
    starting_type, _ = GroupType.objects.get_or_create(name=starting)
    current_type, _ = GroupType.objects.get_or_create(name=current)
    group = PlayerGroup.objects.create(
        match_data=tracker.match_data,
        team=team,
        starting_type=starting_type,
        current_type=current_type,
    )
    group.players.add(*players)
    return group


@pytest.fixture
def roster() -> tuple[TrackerMatchContext, dict[str, Any]]:
    """Mix account, local, private, archived and stale-provider display names."""
    for role in ("Aanval", "Verdediging", "Reserve", "Extra", "Unsupported"):
        GroupType.objects.get_or_create(name=role)
    tracker = create_tracker_match(prefix="Shared roster")
    PlayerGroup.objects.filter(match_data=tracker.match_data).delete()
    expected: dict[str, Any] = {}
    for side, team in (("home", tracker.home_team), ("away", tracker.away_team)):
        account = create_tracker_player(username=f"{side}-account")
        alias = create_tracker_player(username=f"{side}-alias-account")
        alias.name = f"{side} alias"
        alias.save(update_fields=["name"])
        imported = Player.objects.create(
            name=f"{side} imported",
            knkv_person_id=f"synthetic-{side}-visible",
            knkv_privacy="OPEN",
            knkv_observed_at=timezone.now(),
        )
        private = Player.objects.create(
            name="Hidden provider name",
            knkv_person_id=f"synthetic-{side}-private",
            knkv_privacy="PRIVATE",
            knkv_observed_at=timezone.now(),
        )
        stale = Player.objects.create(
            name="Expired provider name",
            knkv_person_id=f"synthetic-{side}-stale",
            knkv_privacy="OPEN",
            knkv_observed_at=timezone.now() - timedelta(days=9),
        )
        archived = create_tracker_player(username=f"{side}-archived-account")
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at"])
        anonymous = Player.objects.create()
        local = Player.objects.create(name=f"{side} local")
        defending = _group(
            tracker, team, "Aanval", "Verdediging", [account, alias, imported, private]
        )
        attacking = _group(tracker, team, "Verdediging", "Aanval", [stale, archived])
        _group(tracker, team, "Reserve", "Aanval", [anonymous, local])
        expected[side] = {
            "groups": [
                (attacking, []),
                (
                    defending,
                    [
                        (account, f"{side}-account"),
                        (alias, f"{side} alias"),
                        (imported, f"{side} imported"),
                    ],
                ),
            ],
            "reserves": [(anonymous, "Afgeschermd"), (local, f"{side} local")],
        }
        ignored = create_tracker_player(username=f"{side}-ignored")
        _group(tracker, team, "Extra", "Unsupported", [ignored])
        _group(tracker, team, "Reserve", "Reserve", [ignored])
    other = create_tracker_match(prefix="Other roster match")
    _group(other, tracker.home_team, "Aanval", "Aanval", [ignored])
    return tracker, expected


@pytest.mark.parametrize("side", ["home", "away"])
@pytest.mark.parametrize("configuration", [False, True])
def test_roster_preserves_roles_names_scope_and_first_reserve(
    roster: tuple[TrackerMatchContext, dict[str, Any]],
    side: str,
    configuration: bool,
) -> None:
    """A role swap or privacy decision must survive a full or compact snapshot."""
    tracker, expected = roster
    team = tracker.home_team if side == "home" else tracker.away_team
    state = tracker_state.get_tracker_state(
        tracker.match, team=team, include_configuration=configuration
    )
    assert [group["id"] for group in state["player_groups"]] == [
        str(group.pk) for group, _ in expected[side]["groups"]
    ]
    for actual, (group, players) in zip(
        state["player_groups"], expected[side]["groups"], strict=True
    ):
        assert actual["starting_type"] == group.starting_type.name
        assert actual["current_type"] == group.current_type.name
        assert {(p["id"], p["name"]) for p in actual["players"]} == {
            (str(player.pk), name) for player, name in players
        }
    assert {(p["id"], p["name"]) for p in state["reserve_players"]} == {
        (str(player.pk), name) for player, name in expected[side]["reserves"]
    }


def test_roster_does_not_hydrate_players_from_discarded_groups(
    roster: tuple[TrackerMatchContext, dict[str, Any]],
) -> None:
    """Unsupported roles and later duplicate reserve groups contribute no players."""
    tracker, expected = roster
    load_player = Mock(wraps=Player.from_db.__func__)
    with patch.object(Player, "from_db", classmethod(load_player)):
        tracker_state.get_tracker_state(
            tracker.match, team=tracker.home_team, include_configuration=False
        )
    expected_count = sum(len(players) for _, players in expected["home"]["groups"])
    expected_count += len(expected["home"]["reserves"])
    assert load_player.call_count == expected_count


@pytest.mark.parametrize("players_per_group", [0, 4, 24])
def test_active_and_reserve_rosters_share_two_selects(players_per_group: int) -> None:
    """Roster size does not add account reads or duplicate group/player queries."""
    for role in ("Aanval", "Verdediging", "Reserve"):
        GroupType.objects.get_or_create(name=role)
    tracker = create_tracker_match(prefix=f"Bounded roster {players_per_group}")
    PlayerGroup.objects.filter(match_data=tracker.match_data).delete()
    for role in ("Aanval", "Verdediging", "Reserve"):
        players = [
            create_tracker_player(username=f"roster-{role}-{index}")
            for index in range(players_per_group)
        ]
        _group(tracker, tracker.home_team, role, role, players)
    with CaptureQueriesContext(connection) as queries:
        state = tracker_state.get_tracker_state(
            tracker.match, team=tracker.home_team, include_configuration=False
        )
    roster_queries = [
        q
        for q in queries
        if any(
            f'FROM "{table}"' in q["sql"]
            for table in ("game_tracker_playergroup", "player_player", "auth_user")
        )
    ]
    assert len(roster_queries) == ROSTER_SELECTS
    assert sum(len(group["players"]) for group in state["player_groups"]) == (
        players_per_group * 2
    )
    assert len(state["reserve_players"]) == players_per_group


@pytest.mark.parametrize("role", [None, "Aanval", "Unsupported"])
def test_roster_without_a_reserve_group(role: str | None) -> None:
    """Missing reserve history and unsupported-only rosters remain valid reads."""
    for name in ("Aanval", "Unsupported"):
        GroupType.objects.get_or_create(name=name)
    tracker = create_tracker_match(prefix="No reserve roster")
    PlayerGroup.objects.filter(match_data=tracker.match_data).delete()
    if role is not None:
        _group(tracker, tracker.home_team, role, role, [])
    state = tracker_state.get_tracker_state(
        tracker.match, team=tracker.home_team, include_configuration=False
    )
    assert len(state["player_groups"]) == int(role == "Aanval")
    assert state["reserve_players"] == []


@pytest.mark.parametrize("side", ["home", "away"])
@pytest.mark.parametrize("source", ["tracker", "knkv", "archive"])
def test_latest_goal_reuses_snapshot_score(side: str, source: str) -> None:
    """Latest goals retain both perspectives and authoritative imported totals."""
    tracker = create_tracker_match(prefix=f"Shared score {source} {side}")
    player = create_tracker_player(username="shared-score-player")
    goal_type = GoalType.objects.create(name="Shared score distance")
    for team in (tracker.home_team, tracker.away_team, tracker.home_team):
        Shot.objects.create(
            match_data=tracker.match_data,
            team=team,
            player=player,
            scored=True,
            shot_type=goal_type,
            time=timezone.now(),
        )
    imported = source != "tracker"
    MatchData.objects.filter(pk=tracker.match_data.pk).update(
        score_source=source,
        status="finished" if imported else "active",
        home_score=9,
        away_score=7,
    )
    team = tracker.home_team if side == "home" else tracker.away_team
    with patch.object(tracker_state, "_score", wraps=tracker_state._score) as score:
        state = tracker_state.get_tracker_state(
            tracker.match, team=team, include_configuration=False
        )
    expected = (9, 7) if imported else (2, 1)
    if side == "away":
        expected = expected[::-1]
    assert state["score"] == {"for": expected[0], "against": expected[1]}
    assert state["last_event"]["type"] == "goal"
    assert state["last_event"]["goals_for"] == expected[0]
    assert state["last_event"]["goals_against"] == expected[1]
    score.assert_called_once()


def test_latest_missed_shot_does_not_read_score_again() -> None:
    """Misses do not include goal totals but still share the snapshot's one read."""
    tracker = create_tracker_match(prefix="Shared score miss")
    Shot.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=create_tracker_player(username="shared-score-miss"),
        scored=False,
        time=timezone.now(),
    )
    with patch.object(tracker_state, "_score", wraps=tracker_state._score) as score:
        state = tracker_state.get_tracker_state(
            tracker.match, team=tracker.home_team, include_configuration=False
        )
    assert state["last_event"]["type"] == "shot"
    assert "goals_for" not in state["last_event"]
    score.assert_called_once()
