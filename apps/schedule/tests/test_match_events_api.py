"""Tests for match events schedule endpoints."""

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    GroupType,
    MatchData,
    MatchPart,
    PlayerChange,
    PlayerGroup,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_events_halftime_substitution_is_serialized_as_rust(
    client: Client,
) -> None:
    """A substitution registered between part 1 and 2 should be shown as half-time.

    Regression test for the "30+..." time label: halftime substitutions should not be
    serialized as added time of part 1.

    """
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.current_part = 2
    match_data.save(update_fields=["status", "current_part"])

    group_type, _created = GroupType.objects.get_or_create(
        name="Aanval",
        defaults={"order": 0},
    )
    player_group = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=group_type,
        current_type=group_type,
    )

    user_model = get_user_model()
    player_out_user = user_model.objects.create_user(
        username="player_out",
        password="pass1234",  # noqa: S106  # nosec
    )
    player_in_user = user_model.objects.create_user(
        username="player_in",
        password="pass1234",  # noqa: S106  # nosec
    )

    base = timezone.now()
    part_1_start = base
    part_1_end = base + timedelta(minutes=30)
    part_2_start = base + timedelta(minutes=40)

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=part_1_start,
        end_time=part_1_end,
        active=False,
    )
    MatchPart.objects.create(
        match_data=match_data,
        part_number=2,
        start_time=part_2_start,
        end_time=None,
        active=False,
    )

    halftime_time = base + timedelta(minutes=35)

    PlayerChange.objects.create(
        player_in=player_in_user.player,
        player_out=player_out_user.player,
        player_group=player_group,
        match_data=match_data,
        match_part=None,
        time=halftime_time,
    )

    response = client.get(f"/api/matches/{match.id_uuid}/events/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert payload["home_team_id"] == str(home_team.id_uuid)

    assert "match_parts" in payload
    part_numbers = {p["part_number"] for p in payload["match_parts"]}
    assert {1, 2}.issubset(part_numbers)
    part_one = next(p for p in payload["match_parts"] if p["part_number"] == 1)
    assert part_one["end_time"] is not None

    substitutes = [e for e in payload["events"] if e.get("type") == "substitute"]
    assert substitutes, "Expected at least one substitute event"

    halftime_sub = substitutes[0]
    assert halftime_sub["time"] == "Rust"
    assert "match_part_id" not in halftime_sub
    assert "time_iso" in halftime_sub
