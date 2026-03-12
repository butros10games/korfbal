# ruff: noqa: D103
"""Match-flow and timeout tests for the tracker HTTP service."""

from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, MatchPart, Pause, Timeout
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


MAX_TIMEOUTS = 2


@pytest.mark.django_db
def test_part_end_closes_active_pause_even_without_active_part() -> None:
    tracker = create_tracker_match(prefix="PartEnd Pause")
    match_data = tracker.match_data
    match_data.status = "active"
    match_data.parts = 2
    match_data.current_part = 1
    match_data.save(update_fields=["status", "parts", "current_part"])

    part = create_match_part(
        match_data=match_data,
        part_number=1,
        active=False,
        start_offset=timedelta(),
        end_offset=timedelta(),
    )
    pause = Pause.objects.create(
        match_data=match_data,
        match_part=part,
        start_time=datetime.now(UTC),
        active=True,
    )

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "part_end"},
    )

    pause.refresh_from_db()
    assert pause.active is False
    assert pause.end_time is not None


@pytest.mark.django_db
def test_substitute_reg_allowed_between_parts_and_next_part_can_start() -> None:
    tracker = create_tracker_match(prefix="BetweenParts")
    match_data = tracker.match_data
    match_data.status = "active"
    match_data.parts = 2
    match_data.current_part = 2
    match_data.save(update_fields=["status", "parts", "current_part"])

    create_match_part(
        match_data=match_data,
        part_number=1,
        active=False,
        start_offset=-timedelta(minutes=30),
        end_offset=-timedelta(minutes=1),
    )

    group_types = create_group_types("Aanval", "Reserve")
    player_out = create_tracker_player(username="bp_player_out")
    player_in = create_tracker_player(username="bp_player_in")

    reserve_group = create_player_group(
        match_data=match_data,
        team=tracker.home_team,
        group_type=group_types["Reserve"],
    )
    active_group = create_player_group(
        match_data=match_data,
        team=tracker.home_team,
        group_type=group_types["Aanval"],
    )
    active_group.players.add(player_out)
    reserve_group.players.add(player_in)

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={
            "command": "substitute_reg",
            "new_player_id": str(player_in.id_uuid),
            "old_player_id": str(player_out.id_uuid),
        },
    )

    active_group.refresh_from_db()
    reserve_group.refresh_from_db()
    assert active_group.players.filter(id_uuid=player_in.id_uuid).exists()
    assert not active_group.players.filter(id_uuid=player_out.id_uuid).exists()
    assert reserve_group.players.filter(id_uuid=player_out.id_uuid).exists()

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )
    assert MatchPart.objects.filter(
        match_data=match_data,
        part_number=2,
        active=True,
    ).exists()


@pytest.mark.django_db
def test_substitute_reg_allows_paused_match() -> None:
    home_club = Club.objects.create(name="Paused Sub Home Club")
    away_club = Club.objects.create(name="Paused Sub Away Club")
    home_team = Team.objects.create(name="Paused Sub Home Team", club=home_club)
    away_team = Team.objects.create(name="Paused Sub Away Team", club=away_club)

    season = Season.objects.create(
        name="Paused Sub Season",
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
    match_data.parts = 2
    match_data.current_part = 1
    match_data.save(update_fields=["status", "parts", "current_part"])

    current_part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC) - timedelta(minutes=5),
        active=True,
    )

    Pause.objects.create(
        match_data=match_data,
        match_part=current_part,
        start_time=datetime.now(UTC) - timedelta(minutes=1),
        active=True,
    )

    group_types = create_group_types("Aanval", "Reserve")
    player_out = (
        get_user_model()
        .objects.create_user(username="paused_player_out", password=TEST_PASSWORD)
        .player
    )
    player_in = (
        get_user_model()
        .objects.create_user(username="paused_player_in", password=TEST_PASSWORD)
        .player
    )

    reserve_group = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Reserve"],
    )
    active_group = create_player_group(
        match_data=match_data,
        team=home_team,
        group_type=group_types["Aanval"],
    )
    active_group.players.add(player_out)
    reserve_group.players.add(player_in)

    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "substitute_reg",
            "new_player_id": str(player_in.id_uuid),
            "old_player_id": str(player_out.id_uuid),
        },
    )

    active_group.refresh_from_db()
    reserve_group.refresh_from_db()
    assert active_group.players.filter(id_uuid=player_in.id_uuid).exists()
    assert reserve_group.players.filter(id_uuid=player_out.id_uuid).exists()


@pytest.mark.django_db
def test_timeout_command_requires_for_team_flag() -> None:
    home_club = Club.objects.create(name="Timeout Req Home Club")
    away_club = Club.objects.create(name="Timeout Req Away Club")
    home_team = Team.objects.create(name="Timeout Req Home Team", club=home_club)
    away_team = Team.objects.create(name="Timeout Req Away Team", club=away_club)

    season = Season.objects.create(
        name="Timeout Req Season",
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

    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(match, team=home_team, payload={"command": "timeout"})

    assert exc.value.code == "bad_request"


@pytest.mark.django_db
def test_timeout_command_can_register_opponent_timeout_and_counts_in_state() -> None:
    home_club = Club.objects.create(name="Timeout Opp Home Club")
    away_club = Club.objects.create(name="Timeout Opp Away Club")
    home_team = Team.objects.create(name="Timeout Opp Home Team", club=home_club)
    away_team = Team.objects.create(name="Timeout Opp Away Team", club=away_club)

    season = Season.objects.create(
        name="Timeout Opp Season",
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

    apply_tracker_command(
        match,
        team=home_team,
        payload={"command": "timeout", "for_team": False},
    )

    assert Timeout.objects.filter(match_data=match_data, team=away_team).count() == 1

    state = get_tracker_state(match, team=home_team)
    assert state["timeouts"]["for"] == 0
    assert state["timeouts"]["against"] == 1
    assert state["timeouts"]["max"] == MAX_TIMEOUTS
