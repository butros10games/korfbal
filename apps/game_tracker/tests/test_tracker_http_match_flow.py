# ruff: noqa: D103
"""Match-flow and timeout tests for the tracker HTTP service."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

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
def test_part_end_rejects_missing_active_part_without_advancing() -> None:
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

    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "part_end"},
        )

    pause.refresh_from_db()
    match_data.refresh_from_db()
    assert exc.value.code == "no_active_part"
    assert pause.active is True
    assert pause.end_time is None
    assert match_data.current_part == 1


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


@pytest.mark.django_db
def test_timeout_command_enforces_maximum_for_each_team() -> None:
    tracker = create_tracker_match(prefix="Timeout Limit")
    match_data = tracker.match_data
    match_data.status = "active"
    match_data.save(update_fields=["status"])
    part = create_match_part(match_data=match_data)
    Timeout.objects.bulk_create([
        Timeout(match_data=match_data, match_part=part, team=tracker.home_team),
        Timeout(match_data=match_data, match_part=part, team=tracker.home_team),
    ])

    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "timeout", "for_team": True},
        )

    assert exc.value.code == "max_timeouts"
    assert Timeout.objects.filter(match_data=match_data).count() == MAX_TIMEOUTS


@pytest.mark.django_db
def test_duplicate_part_end_does_not_skip_the_next_part() -> None:
    tracker = create_tracker_match(prefix="Duplicate Part End")
    match_data = tracker.match_data
    match_data.status = "active"
    match_data.parts = 2
    match_data.save(update_fields=["status", "parts"])
    create_match_part(match_data=match_data)

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "part_end"},
    )
    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "part_end"},
        )

    match_data.refresh_from_db()
    assert exc.value.code == "no_active_part"
    assert match_data.current_part == match_data.parts
    assert match_data.status == "active"


@pytest.mark.django_db
def test_finished_match_cannot_be_restarted() -> None:
    tracker = create_tracker_match(prefix="Finished Restart")
    tracker.match_data.status = "finished"
    tracker.match_data.save(update_fields=["status"])

    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "start/pause"},
        )

    assert exc.value.code == "match_finished"
    assert not MatchPart.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db
def test_timer_commands_ignore_skewed_client_time() -> None:
    tracker = create_tracker_match(prefix="Server Clock")
    client_time = datetime.now(UTC) + timedelta(minutes=4)

    before = timezone.now()
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={
            "command": "start/pause",
            "client_time_iso": client_time.isoformat(),
        },
    )
    after = timezone.now()

    part = MatchPart.objects.get(match_data=tracker.match_data, active=True)
    assert before <= part.start_time <= after


@pytest.mark.django_db
def test_tracker_rejects_a_team_outside_the_match() -> None:
    tracker = create_tracker_match(prefix="Invalid Perspective")
    unrelated_team = Team.objects.create(
        name="Unrelated Team",
        club=Club.objects.create(name="Unrelated Club"),
    )

    with pytest.raises(TrackerCommandError) as exc:
        get_tracker_state(tracker.match, team=unrelated_team)

    assert exc.value.code == "invalid_team"


@pytest.mark.django_db(transaction=True)
def test_finished_task_is_enqueued_after_match_commit() -> None:
    tracker = create_tracker_match(prefix="Finish Commit")
    match_data = tracker.match_data
    match_data.status = "active"
    match_data.parts = 1
    match_data.save(update_fields=["status", "parts"])
    create_match_part(match_data=match_data)

    with patch("apps.player.tasks.handle_match_finished.delay") as delay:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "part_end"},
        )

    match_data.refresh_from_db()
    assert match_data.status == "finished"
    delay.assert_called_once_with(
        match_id=str(tracker.match.id_uuid),
        match_data_id=str(match_data.id_uuid),
    )
