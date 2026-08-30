"""Integration tests for shared match statistics and timer payloads."""

from __future__ import annotations

from datetime import timedelta
import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, MatchPart, Pause, Shot
from apps.kwt_common.utils.general_stats import general_stats
from apps.kwt_common.utils.time_utils import get_time, get_time_display
from apps.schedule.models import Match, Season
from apps.team.models import Team


def _match_data() -> tuple[MatchData, Team, Team]:
    today = timezone.localdate()
    season = Season.objects.create(
        name="Shared utility tests",
        start_date=today,
        end_date=today + timedelta(days=1),
    )
    home = Team.objects.create(
        name="Utility Home",
        club=Club.objects.create(name="Utility Home Club"),
    )
    away = Team.objects.create(
        name="Utility Away",
        club=Club.objects.create(name="Utility Away Club"),
    )
    match = Match.objects.create(
        home_team=home,
        away_team=away,
        season=season,
        start_time=timezone.now(),
    )
    return MatchData.objects.get(match_link=match), home, away


@pytest.mark.django_db
def test_general_stats_counts_for_and_against_by_goal_type() -> None:
    """Stats preserve the tracked-team direction and include empty goal types."""
    match_data, home, away = _match_data()
    player = get_user_model().objects.create_user(username="stats-player").player
    free_throw = GoalType.objects.create(name="Free throw")
    GoalType.objects.create(name="Penalty")

    Shot.objects.bulk_create([
        Shot(
            match_data=match_data,
            player=player,
            team=home,
            for_team=True,
            scored=True,
            shot_type=free_throw,
        ),
        Shot(
            match_data=match_data,
            player=player,
            team=home,
            for_team=True,
            scored=False,
        ),
        Shot(
            match_data=match_data,
            player=player,
            team=away,
            for_team=False,
            scored=True,
            shot_type=free_throw,
        ),
        Shot(
            match_data=match_data,
            player=player,
            team=away,
            for_team=False,
            scored=False,
        ),
    ])

    payload = json.loads(async_to_sync(general_stats)([match_data]))

    assert payload["command"] == "stats"
    assert payload["data"]["type"] == "general"
    assert payload["data"]["stats"] == {
        "shots_for": 2,
        "shots_against": 2,
        "goals_for": 1,
        "goals_against": 1,
        "team_goal_stats": {
            "Free throw": {"goals_by_player": 1, "goals_against_player": 1},
            "Penalty": {"goals_by_player": 0, "goals_against_player": 0},
        },
        "goal_types": [
            {"id": str(free_throw.id_uuid), "name": "Free throw"},
            {
                "id": str(GoalType.objects.get(name="Penalty").id_uuid),
                "name": "Penalty",
            },
        ],
    }


@pytest.mark.django_db
def test_get_time_reports_deactivated_match_without_active_part() -> None:
    """Clients receive a deactivated payload when no period is active."""
    match_data, _home, _away = _match_data()
    current_part = MatchPart(
        match_data=match_data,
        part_number=1,
        start_time=timezone.now(),
    )

    payload = json.loads(async_to_sync(get_time)(match_data, current_part))

    assert payload == {
        "command": "timer_data",
        "type": "deactivated",
        "match_data_id": str(match_data.id_uuid),
    }


@pytest.mark.django_db
def test_get_time_reports_active_pause_and_completed_pause_duration() -> None:
    """The timer payload separates the active pause from elapsed pauses."""
    completed_pause_seconds = 12.0
    match_data, _home, _away = _match_data()
    part_start = timezone.now() - timedelta(minutes=5)
    part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=part_start,
        active=True,
    )
    Pause.objects.create(
        match_data=match_data,
        match_part=part,
        start_time=part_start + timedelta(minutes=1),
        end_time=part_start + timedelta(minutes=1, seconds=12),
        active=False,
    )
    active_pause_start = part_start + timedelta(minutes=4)
    Pause.objects.create(
        match_data=match_data,
        match_part=part,
        start_time=active_pause_start,
        active=True,
    )

    payload = json.loads(async_to_sync(get_time)(match_data, part))

    assert payload["type"] == "pause"
    assert payload["time"] == part_start.isoformat()
    assert payload["calc_to"] == active_pause_start.isoformat()
    assert payload["pause_length"] == pytest.approx(completed_pause_seconds)
    assert payload["length"] == match_data.part_length


def test_time_display_formats_full_match_minutes_and_seconds() -> None:
    """Durations are formatted as zero-padded minutes and seconds."""
    match_data = MatchData(part_length=3599)

    assert get_time_display(match_data) == "59:59"
