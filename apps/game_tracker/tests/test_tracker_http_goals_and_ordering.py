# ruff: noqa: D103
"""Goal-swap and ordering tests for the tracker HTTP service."""

from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, MatchPart
from apps.game_tracker.services.tracker_http import (
    apply_tracker_command,
    get_tracker_state,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    TEST_PASSWORD,
    create_group_types,
    create_player_group,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_goal_reg_swaps_attack_defense_every_two_goals() -> None:
    home_club = Club.objects.create(name="Swap Home Club")
    away_club = Club.objects.create(name="Swap Away Club")
    home_team = Team.objects.create(name="Swap Home Team", club=home_club)
    away_team = Team.objects.create(name="Swap Away Team", club=away_club)

    season = Season.objects.create(
        name="Swap Season",
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

    goal_type = GoalType.objects.create(name="Doorloop")
    group_types = create_group_types("Aanval", "Verdediging")

    home_scorer = (
        get_user_model()
        .objects.create_user(username="home_scorer_swap", password=TEST_PASSWORD)
        .player
    )
    away_scorer = (
        get_user_model()
        .objects.create_user(username="away_scorer_swap", password=TEST_PASSWORD)
        .player
    )

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
    home_club = Club.objects.create(name="Revert Home Club")
    away_club = Club.objects.create(name="Revert Away Club")
    home_team = Team.objects.create(name="Revert Home Team", club=home_club)
    away_team = Team.objects.create(name="Revert Away Team", club=away_club)

    season = Season.objects.create(
        name="Revert Season",
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

    goal_type = GoalType.objects.create(name="Vrijebal")
    group_types = create_group_types("Aanval", "Verdediging")

    home_scorer = (
        get_user_model()
        .objects.create_user(username="home_scorer_revert", password=TEST_PASSWORD)
        .player
    )
    away_scorer = (
        get_user_model()
        .objects.create_user(username="away_scorer_revert", password=TEST_PASSWORD)
        .player
    )

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
def test_client_time_keeps_last_event_order_stable() -> None:
    home_club = Club.objects.create(name="ClientTime Home Club")
    away_club = Club.objects.create(name="ClientTime Away Club")
    home_team = Team.objects.create(name="ClientTime Home Team", club=home_club)
    away_team = Team.objects.create(name="ClientTime Away Team", club=away_club)

    season = Season.objects.create(
        name="ClientTime Season",
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
    match_data.current_part = 1
    match_data.save(update_fields=["status", "current_part"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

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
    assert state["last_event"]["type"] == "attack"
    assert state["last_event"]["time_iso"] == late.isoformat()
