# ruff: noqa: D103
"""Substitution-count and query-count tests for the tracker HTTP service."""

from django.db import connection
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import PlayerChange
from apps.game_tracker.services.tracker_commands import TrackerCommandError
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)


MAX_WISSELS = 8


@pytest.mark.django_db
def test_tracker_state_includes_substitutions_total() -> None:
    tracker = create_tracker_match(prefix="Sub")
    match = tracker.match
    match_data = tracker.match_data
    home_team = tracker.home_team
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    create_match_part(match_data=match_data)

    group_types = create_group_types("Aanval", "Verdediging", "Reserve")
    player_out = create_tracker_player(username="sub_player_out")
    player_in = create_tracker_player(username="sub_player_in")

    pg_attack = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Aanval"],
    )
    create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Verdediging"],
    )
    pg_reserve = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Reserve"],
    )

    pg_attack.players.add(player_out)
    pg_reserve.players.add(player_in)

    initial_state = get_tracker_state(match, team=home_team)
    assert initial_state["substitutions_total"] == 0
    assert initial_state["substitutions"]["for"] == 0
    assert initial_state["substitutions"]["against"] == 0
    assert initial_state["substitutions"]["max"] == MAX_WISSELS

    next_state = apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "substitute_reg",
            "new_player_id": str(player_in.id_uuid),
            "old_player_id": str(player_out.id_uuid),
        },
    )

    assert next_state["substitutions_total"] == 1
    assert next_state["substitutions"]["for"] == 1


@pytest.mark.django_db
def test_substitute_reg_enforces_max_wissels_per_team() -> None:
    tracker = create_tracker_match(prefix="MaxSub")
    match = tracker.match
    match_data = tracker.match_data
    home_team = tracker.home_team
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    create_match_part(match_data=match_data)

    group_types = create_group_types("Aanval", "Reserve")
    player_a = create_tracker_player(username="max_sub_a")
    player_b = create_tracker_player(username="max_sub_b")

    pg_attack = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Aanval"],
    )
    pg_reserve = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Reserve"],
    )

    pg_attack.players.add(player_a)
    pg_reserve.players.add(player_b)

    PlayerChange.objects.bulk_create(
        PlayerChange(
            player_in=player_b,
            player_out=player_a,
            player_group=pg_attack,
            match_data=match_data,
        )
        for _ in range(MAX_WISSELS)
    )

    state_after = get_tracker_state(match, team=home_team)
    assert state_after["substitutions"]["for"] == MAX_WISSELS

    with pytest.raises(TrackerCommandError):
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "substitute_reg",
                "new_player_id": str(player_b.id_uuid),
                "old_player_id": str(player_a.id_uuid),
            },
        )


@pytest.mark.django_db
def test_substitute_against_reg_registers_opponent_wissel_without_players() -> None:
    tracker = create_tracker_match(prefix="OppSub")
    match = tracker.match
    match_data = tracker.match_data
    home_team = tracker.home_team
    away_team = tracker.away_team
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    create_match_part(match_data=match_data)

    group_types = create_group_types("Reserve")
    create_player_group(
        match_data=match_data,
        team=away_team,
        group_type=group_types["Reserve"],
    )

    initial_state = get_tracker_state(match, team=home_team)
    assert initial_state["substitutions"]["against"] == 0

    next_state = apply_tracker_command(
        match,
        team=home_team,
        payload={"command": "substitute_against_reg"},
    )

    assert next_state["substitutions"]["against"] == 1

    change = (
        PlayerChange.objects
        .filter(match_data=match_data, player_group__team=away_team)
        .order_by("-time")
        .first()
    )
    assert change is not None
    assert change.player_in is None
    assert change.player_out is None


@pytest.mark.django_db
def test_substitute_against_reg_enforces_max_wissels_for_opponent() -> None:
    tracker = create_tracker_match(prefix="OppSubMax")
    match = tracker.match
    match_data = tracker.match_data
    home_team = tracker.home_team
    away_team = tracker.away_team
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    create_match_part(match_data=match_data)

    group_types = create_group_types("Reserve")
    opponent_reserve = create_player_group(
        match_data=match_data,
        team=away_team,
        group_type=group_types["Reserve"],
    )

    PlayerChange.objects.bulk_create(
        PlayerChange(
            player_group=opponent_reserve,
            match_data=match_data,
        )
        for _ in range(MAX_WISSELS)
    )

    state_after = get_tracker_state(match, team=home_team)
    assert state_after["substitutions"]["against"] == MAX_WISSELS

    with pytest.raises(TrackerCommandError):
        apply_tracker_command(
            match,
            team=home_team,
            payload={"command": "substitute_against_reg"},
        )


@pytest.mark.django_db
def test_get_tracker_state_query_count_does_not_scale_with_players() -> None:
    tracker = create_tracker_match(prefix="State Queries")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(match_data=tracker.match_data, part_number=1)

    group_types = create_group_types("Aanval", "Verdediging", "Reserve")
    attack_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Aanval"],
    )
    defense_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Verdediging"],
    )
    reserve_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Reserve"],
    )

    attack_group.players.add(create_tracker_player(username="state_attack_1"))
    defense_group.players.add(create_tracker_player(username="state_defense_1"))
    reserve_group.players.add(create_tracker_player(username="state_reserve_1"))

    with CaptureQueriesContext(connection) as baseline_queries:
        get_tracker_state(tracker.match, team=tracker.home_team)

    for suffix in range(2, 5):
        attack_group.players.add(
            create_tracker_player(username=f"state_attack_{suffix}")
        )
        defense_group.players.add(
            create_tracker_player(username=f"state_defense_{suffix}")
        )
        reserve_group.players.add(
            create_tracker_player(username=f"state_reserve_{suffix}")
        )

    with CaptureQueriesContext(connection) as expanded_queries:
        get_tracker_state(tracker.match, team=tracker.home_team)

    assert len(expanded_queries) == len(baseline_queries)
