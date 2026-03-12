# ruff: noqa: D103
"""Substitution-count and query-count tests for the tracker HTTP service."""

from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, MatchPart, PlayerChange
from apps.game_tracker.services.tracker_http import (
    TrackerCommandError,
    apply_tracker_command,
    get_tracker_state,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    TEST_PASSWORD,
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team


MAX_WISSELS = 8


@pytest.mark.django_db
def test_tracker_state_includes_substitutions_total() -> None:
    home_club = Club.objects.create(name="Sub Home Club")
    away_club = Club.objects.create(name="Sub Away Club")
    home_team = Team.objects.create(name="Sub Home Team", club=home_club)
    away_team = Team.objects.create(name="Sub Away Team", club=away_club)

    season = Season.objects.create(
        name="Sub Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    group_types = create_group_types("Aanval", "Verdediging", "Reserve")
    player_out = (
        get_user_model()
        .objects.create_user(username="sub_player_out", password=TEST_PASSWORD)
        .player
    )
    player_in = (
        get_user_model()
        .objects.create_user(username="sub_player_in", password=TEST_PASSWORD)
        .player
    )

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
    home_club = Club.objects.create(name="MaxSub Home Club")
    away_club = Club.objects.create(name="MaxSub Away Club")
    home_team = Team.objects.create(name="MaxSub Home Team", club=home_club)
    away_team = Team.objects.create(name="MaxSub Away Team", club=away_club)

    season = Season.objects.create(
        name="MaxSub Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    group_types = create_group_types("Aanval", "Reserve")
    player_a = (
        get_user_model()
        .objects.create_user(username="max_sub_a", password=TEST_PASSWORD)
        .player
    )
    player_b = (
        get_user_model()
        .objects.create_user(username="max_sub_b", password=TEST_PASSWORD)
        .player
    )

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

    for idx in range(MAX_WISSELS):
        new_player = player_b if idx % 2 == 0 else player_a
        old_player = player_a if idx % 2 == 0 else player_b
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "substitute_reg",
                "new_player_id": str(new_player.id_uuid),
                "old_player_id": str(old_player.id_uuid),
            },
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
    home_club = Club.objects.create(name="OppSub Home Club")
    away_club = Club.objects.create(name="OppSub Away Club")
    home_team = Team.objects.create(name="OppSub Home Team", club=home_club)
    away_team = Team.objects.create(name="OppSub Away Team", club=away_club)

    season = Season.objects.create(
        name="OppSub Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

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
    home_club = Club.objects.create(name="OppSubMax Home Club")
    away_club = Club.objects.create(name="OppSubMax Away Club")
    home_team = Team.objects.create(name="OppSubMax Home Team", club=home_club)
    away_team = Team.objects.create(name="OppSubMax Away Team", club=away_club)

    season = Season.objects.create(
        name="OppSubMax Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    group_types = create_group_types("Reserve")
    create_player_group(
        match_data=match_data,
        team=away_team,
        group_type=group_types["Reserve"],
    )

    for _ in range(MAX_WISSELS):
        apply_tracker_command(
            match,
            team=home_team,
            payload={"command": "substitute_against_reg"},
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
