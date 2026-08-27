"""Regression coverage for rebuildable lineup projections."""

from __future__ import annotations

from importlib import import_module

from django.apps import apps as django_apps
from django.utils import timezone
import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import (
    MatchPart,
    PlayerChange,
    PlayerGroup,
    Shot,
    StartingPlayerAssignment,
)
from apps.game_tracker.services.lineup_projections import (
    capture_starting_lineup,
    rebuild_current_lineup,
    rebuild_group_roles,
    rebuild_match_projections,
    starting_group_ids_by_player,
)
from apps.game_tracker.services.match_impact_timeline import (
    build_match_player_role_timeline,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_group_types,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import Player


EXPECTED_TWO_PLAYER_LINEUP = 2
FINAL_PART_NUMBER = 2


def _lineup_fixture() -> tuple[
    TrackerMatchContext,
    PlayerGroup,
    PlayerGroup,
    PlayerGroup,
    tuple[Player, Player],
]:
    tracker = create_tracker_match(prefix="Lineup projection")
    group_types = create_group_types("Aanval", "Verdediging", "Reserve")
    attack = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Aanval"],
    )
    defense = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Verdediging"],
    )
    reserve = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Reserve"],
    )
    starter = create_tracker_player(username="lineup-starter")
    substitute = create_tracker_player(username="lineup-substitute")
    attack.players.add(starter)
    reserve.players.add(substitute)
    return tracker, attack, defense, reserve, (starter, substitute)


@pytest.mark.django_db
def test_first_match_start_captures_immutable_lineup() -> None:
    """Starting assignments are captured once before the first period begins."""
    tracker, attack, _defense, reserve, players = _lineup_fixture()
    starter, substitute = players

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )
    assert starting_group_ids_by_player(tracker.match_data) == {
        str(starter.pk): str(attack.pk),
        str(substitute.pk): str(reserve.pk),
    }

    attack.players.remove(starter)
    reserve.players.add(starter)
    assert capture_starting_lineup(tracker.match_data) == 0
    assert (
        StartingPlayerAssignment.objects.get(
            match_data=tracker.match_data,
            player=starter,
        ).player_group
        == attack
    )


@pytest.mark.django_db
def test_current_lineup_rebuilds_from_snapshot_and_substitutions() -> None:
    """Projection repair produces the same membership after corruption or undo."""
    tracker, attack, _defense, reserve, players = _lineup_fixture()
    starter, substitute = players
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={
            "command": "substitute_reg",
            "new_player_id": str(substitute.pk),
            "old_player_id": str(starter.pk),
        },
    )
    assert attack.players.filter(pk=substitute.pk).exists()
    assert reserve.players.filter(pk=starter.pk).exists()

    attack.players.clear()
    reserve.players.clear()
    rebuild_current_lineup(tracker.match_data)
    assert set(attack.players.values_list("pk", flat=True)) == {substitute.pk}
    assert set(reserve.players.values_list("pk", flat=True)) == {starter.pk}

    PlayerChange.objects.filter(match_data=tracker.match_data).delete()
    rebuild_current_lineup(tracker.match_data)
    assert set(attack.players.values_list("pk", flat=True)) == {starter.pk}
    assert set(reserve.players.values_list("pk", flat=True)) == {substitute.pk}


@pytest.mark.django_db
def test_group_roles_rebuild_from_goal_parity() -> None:
    """Manual goal corrections can restore attack/defence projection exactly."""
    tracker, attack, defense, _reserve, players = _lineup_fixture()
    starter, _substitute = players
    for _index in range(2):
        Shot.objects.create(
            player=starter,
            match_data=tracker.match_data,
            team=tracker.home_team,
            scored=True,
            time=timezone.now(),
        )

    rebuild_group_roles(tracker.match_data)
    attack.refresh_from_db()
    defense.refresh_from_db()
    assert attack.current_type.name == "Verdediging"
    assert defense.current_type.name == "Aanval"

    goal = Shot.objects.filter(match_data=tracker.match_data).first()
    assert goal is not None
    goal.delete()
    rebuild_group_roles(tracker.match_data)
    attack.refresh_from_db()
    defense.refresh_from_db()
    assert attack.current_type == attack.starting_type
    assert defense.current_type == defense.starting_type


@pytest.mark.django_db
def test_all_match_projections_rebuild_from_event_facts() -> None:
    """Score and lifecycle state can be repaired without trusting cached fields."""
    tracker, _attack, _defense, _reserve, players = _lineup_fixture()
    scorer, _substitute = players
    part_one = MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=timezone.now(),
        active=True,
    )
    Shot.objects.create(
        player=scorer,
        match_data=tracker.match_data,
        match_part=part_one,
        team=tracker.home_team,
        scored=True,
        time=part_one.start_time,
    )
    Shot.objects.create(
        player=scorer,
        match_data=tracker.match_data,
        match_part=part_one,
        team=tracker.away_team,
        for_team=False,
        scored=True,
        time=part_one.start_time,
    )
    tracker.match_data.home_score = 99
    tracker.match_data.away_score = 98
    tracker.match_data.status = "finished"
    tracker.match_data.current_part = 2
    tracker.match_data.save(
        update_fields=["home_score", "away_score", "status", "current_part"]
    )

    rebuild_match_projections(tracker.match_data)

    tracker.match_data.refresh_from_db()
    assert (tracker.match_data.home_score, tracker.match_data.away_score) == (1, 1)
    assert tracker.match_data.status == "active"
    assert tracker.match_data.current_part == 1

    part_one.active = False
    part_one.end_time = timezone.now()
    part_one.save(update_fields=["active", "end_time"])
    part_two = MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=2,
        start_time=timezone.now(),
        end_time=timezone.now(),
        active=False,
    )
    assert part_two.end_time is not None
    rebuild_match_projections(tracker.match_data)
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.status == "finished"
    assert tracker.match_data.current_part == FINAL_PART_NUMBER


@pytest.mark.django_db
def test_migration_backfill_reverses_historical_substitutions() -> None:
    """Historical end-state membership is converted to the true starting state."""
    tracker, attack, _defense, reserve, players = _lineup_fixture()
    starter, substitute = players
    tracker.match_data.status = "finished"
    tracker.match_data.save(update_fields=["status"])
    attack.players.set([substitute])
    reserve.players.set([starter])
    PlayerChange.objects.create(
        match_data=tracker.match_data,
        player_group=attack,
        player_in=substitute,
        player_out=starter,
        time=timezone.now(),
    )

    migration = import_module(
        "apps.game_tracker.migrations.0025_startingplayerassignment_and_more"
    )
    migration.backfill_starting_assignments(django_apps, None)

    assert starting_group_ids_by_player(tracker.match_data) == {
        str(starter.pk): str(attack.pk),
        str(substitute.pk): str(reserve.pk),
    }


@pytest.mark.django_db
def test_migration_does_not_freeze_upcoming_lineup() -> None:
    """Deployment leaves provisional groups editable until the first match start."""
    tracker, attack, _defense, reserve, players = _lineup_fixture()
    starter, substitute = players
    migration = import_module(
        "apps.game_tracker.migrations.0025_startingplayerassignment_and_more"
    )

    migration.backfill_starting_assignments(django_apps, None)
    assert (
        StartingPlayerAssignment.objects.filter(match_data=tracker.match_data).exists()
        is False
    )

    attack.players.set([substitute])
    reserve.players.set([starter])
    assert capture_starting_lineup(tracker.match_data) == EXPECTED_TWO_PLAYER_LINEUP
    assert starting_group_ids_by_player(tracker.match_data) == {
        str(substitute.pk): str(attack.pk),
        str(starter.pk): str(reserve.pk),
    }


@pytest.mark.django_db
def test_migration_reverses_null_timestamp_substitution_first() -> None:
    """Backfill reverses the runtime nulls-last replay order exactly."""
    tracker, attack, _defense, reserve, players = _lineup_fixture()
    starter, first_substitute = players
    final_substitute = create_tracker_player(username="lineup-final-substitute")
    reserve.players.add(final_substitute)
    tracker.match_data.status = "finished"
    tracker.match_data.save(update_fields=["status"])
    attack.players.set([final_substitute])
    reserve.players.set([starter, first_substitute])
    PlayerChange.objects.create(
        match_data=tracker.match_data,
        player_group=attack,
        player_in=first_substitute,
        player_out=starter,
        time=timezone.now(),
    )
    PlayerChange.objects.create(
        match_data=tracker.match_data,
        player_group=attack,
        player_in=final_substitute,
        player_out=first_substitute,
        time=None,
    )
    migration = import_module(
        "apps.game_tracker.migrations.0025_startingplayerassignment_and_more"
    )

    migration.backfill_starting_assignments(django_apps, None)

    assert starting_group_ids_by_player(tracker.match_data) == {
        str(starter.pk): str(attack.pk),
        str(first_substitute.pk): str(reserve.pk),
        str(final_substitute.pk): str(reserve.pk),
    }


@pytest.mark.django_db
def test_role_timeline_prefers_start_snapshot_over_mutable_end_state() -> None:
    """Derived minutes and impact no longer infer the start from current groups."""
    tracker, attack, _defense, reserve, players = _lineup_fixture()
    starter, _substitute = players
    capture_starting_lineup(tracker.match_data)
    attack.players.remove(starter)
    reserve.players.add(starter)
    match_end_minutes = 20.0

    timeline = build_match_player_role_timeline(
        known_player_ids=[str(starter.pk)],
        groups=[attack, reserve],
        events=[],
        match_end_minutes=match_end_minutes,
        starting_group_id_by_player=starting_group_ids_by_player(tracker.match_data),
    )

    assert timeline[str(starter.pk)].aanval[0].start == 0
    assert timeline[str(starter.pk)].aanval[0].end == match_end_minutes
    assert timeline[str(starter.pk)].reserve == []
