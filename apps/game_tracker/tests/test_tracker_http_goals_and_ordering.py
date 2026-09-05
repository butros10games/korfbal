# ruff: noqa: D103
"""Goal-swap and ordering tests for the tracker HTTP service."""

from datetime import UTC, datetime, timedelta

import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import GoalType, Shot
from apps.game_tracker.services.tracker_http import (
    TrackerCommandError,
    get_tracker_state,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db
def test_goal_reg_swaps_attack_defense_every_two_goals() -> None:
    tracker = create_tracker_match(prefix="Swap")
    match = tracker.match
    match_data = tracker.match_data
    home_team = tracker.home_team
    away_team = tracker.away_team
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    create_match_part(match_data=match_data)

    goal_type = GoalType.objects.create(name="Doorloop")
    group_types = create_group_types("Aanval", "Verdediging")

    home_scorer = create_tracker_player(username="home_scorer_swap")
    away_scorer = create_tracker_player(username="away_scorer_swap")

    home_pg_attack = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Aanval"],
    )
    home_pg_defense = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Verdediging"],
    )
    home_pg_attack.players.add(home_scorer)

    away_pg_attack = create_player_group(
        match_data=match_data,
        team=away_team,
        group_type=group_types["Aanval"],
    )
    away_pg_defense = create_player_group(
        match_data=match_data,
        team=away_team,
        group_type=group_types["Verdediging"],
    )
    away_pg_attack.players.add(away_scorer)

    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "goal_reg",
            "player_id": str(home_scorer.id_uuid),
            "goal_type": str(goal_type.id_uuid),
            "for_team": True,
        },
    )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()
    away_pg_attack.refresh_from_db()
    away_pg_defense.refresh_from_db()

    assert home_pg_attack.current_type.name == "Aanval"
    assert home_pg_defense.current_type.name == "Verdediging"
    assert away_pg_attack.current_type.name == "Aanval"
    assert away_pg_defense.current_type.name == "Verdediging"

    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "goal_reg",
            "player_id": str(home_scorer.id_uuid),
            "goal_type": str(goal_type.id_uuid),
            "for_team": True,
        },
    )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()
    away_pg_attack.refresh_from_db()
    away_pg_defense.refresh_from_db()

    assert home_pg_attack.current_type.name == "Verdediging"
    assert home_pg_defense.current_type.name == "Aanval"
    assert away_pg_attack.current_type.name == "Verdediging"
    assert away_pg_defense.current_type.name == "Aanval"


@pytest.mark.django_db
def test_remove_last_event_reverts_swap_when_goal_removed() -> None:
    tracker = create_tracker_match(prefix="Revert")
    match = tracker.match
    match_data = tracker.match_data
    home_team = tracker.home_team
    away_team = tracker.away_team
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    create_match_part(match_data=match_data)

    goal_type = GoalType.objects.create(name="Vrijebal")
    group_types = create_group_types("Aanval", "Verdediging")

    home_scorer = create_tracker_player(username="home_scorer_revert")
    away_scorer = create_tracker_player(username="away_scorer_revert")

    home_pg_attack = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Aanval"],
    )
    home_pg_defense = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Verdediging"],
    )
    home_pg_attack.players.add(home_scorer)

    away_pg_attack = create_player_group(
        match_data=match_data,
        team=away_team,
        group_type=group_types["Aanval"],
    )
    create_player_group(
        match_data=match_data,
        team=away_team,
        group_type=group_types["Verdediging"],
    )
    away_pg_attack.players.add(away_scorer)

    for _ in range(2):
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "goal_reg",
                "player_id": str(home_scorer.id_uuid),
                "goal_type": str(goal_type.id_uuid),
                "for_team": True,
            },
        )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()
    assert home_pg_attack.current_type.name == "Verdediging"
    assert home_pg_defense.current_type.name == "Aanval"

    apply_tracker_command(
        match,
        team=home_team,
        payload={"command": "remove_last_event"},
    )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()
    assert home_pg_attack.current_type.name == "Aanval"
    assert home_pg_defense.current_type.name == "Verdediging"


@pytest.mark.django_db
def test_commit_sequence_keeps_last_event_order_stable() -> None:
    tracker = create_tracker_match(prefix="ClientTime")
    match = tracker.match
    match_data = tracker.match_data
    home_team = tracker.home_team
    match_data.status = "active"
    match_data.current_part = 1
    match_data.save(update_fields=["status", "current_part"])

    create_match_part(match_data=match_data)

    base = datetime.now(UTC).replace(microsecond=0)
    late = base + timedelta(seconds=2)
    early = base - timedelta(seconds=2)

    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "new_attack",
            "client_time_ms": int(late.timestamp() * 1000),
        },
    )
    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "timeout",
            "for_team": True,
            "client_time_ms": int(early.timestamp() * 1000),
        },
    )

    state = get_tracker_state(match, team=home_team)
    assert state["last_event"]["type"] == "pause"
    assert state["last_event"]["event_kind"] == "timeout"


@pytest.mark.django_db
def test_goal_registration_rejects_player_outside_match_roster() -> None:
    tracker = create_tracker_match(prefix="Roster Boundary")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(match_data=tracker.match_data)
    goal_type = GoalType.objects.create(name="Roster Boundary Goal")
    outsider = create_tracker_player(username="roster_outsider")

    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "goal_reg",
                "player_id": str(outsider.id_uuid),
                "goal_type": str(goal_type.id_uuid),
                "for_team": True,
            },
        )

    assert exc.value.code == "bad_request"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("command", "scored"),
    [("goal_reg", True), ("shot_reg", False)],
)
def test_against_event_accepts_defender_from_tracked_team_roster(
    command: str,
    scored: bool,
) -> None:
    tracker = create_tracker_match(prefix="Goal Against Roster")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(match_data=tracker.match_data)
    goal_type = GoalType.objects.create(name="Goal Against Type")
    defense_type = create_group_types("Verdediging")["Verdediging"]
    defense_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=defense_type,
    )
    defender = create_tracker_player(username="goal_against_defender")
    defense_group.players.add(defender)

    state = get_tracker_state(tracker.match, team=tracker.home_team)
    assert any(
        player["id"] == str(defender.id_uuid)
        for group in state["player_groups"]
        for player in group["players"]
    )

    payload = {
        "command": command,
        "player_id": str(defender.id_uuid),
        "for_team": False,
    }
    payload["goal_type" if scored else "shot_type"] = str(goal_type.id_uuid)
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload=payload,
    )

    shot = Shot.objects.get(match_data=tracker.match_data)
    assert shot.player == defender
    assert shot.team == tracker.away_team
    assert shot.for_team is False
    assert shot.scored is scored
