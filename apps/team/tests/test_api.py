"""Tests for the team API endpoints."""

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, Season
from apps.team.models import Team
from apps.team.models.team_data import TeamData


@pytest.mark.django_db
def test_team_overview_includes_matches_stats_and_roster(  # noqa: PLR0914
    client: Client,
) -> None:
    """Ensure the overview endpoint returns aggregated data for the new frontend."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )
    previous_season = Season.objects.create(
        name="2024",
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=35),
    )

    club = Club.objects.create(name="Team Club")
    opponent_club = Club.objects.create(name="Opponent Club")
    team = Team.objects.create(name="Team 1", club=club)
    opponent_team = Team.objects.create(name="Opponent 1", club=opponent_club)

    user = get_user_model().objects.create_user(
        username="player",
        password="pass1234",  # noqa: S106
    )
    player = user.player

    team_data = TeamData.objects.create(team=team, season=season)
    team_data.players.add(player)
    legacy_team_data = TeamData.objects.create(team=team, season=previous_season)
    legacy_team_data.players.add(player)

    future_match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() + timedelta(days=3),
    )
    past_match = Match.objects.create(
        home_team=opponent_team,
        away_team=team,
        season=season,
        start_time=timezone.now() - timedelta(days=5),
    )

    MatchData.objects.create(
        match_link=future_match,
        status="upcoming",
        home_score=0,
        away_score=0,
    )
    MatchData.objects.create(
        match_link=past_match,
        status="finished",
        home_score=21,
        away_score=18,
    )

    legacy_match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=previous_season,
        start_time=timezone.now() - timedelta(days=200),
    )
    MatchData.objects.create(
        match_link=legacy_match,
        status="finished",
        home_score=18,
        away_score=16,
    )

    response = client.get(f"/api/team/teams/{team.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["team"]["id_uuid"] == str(team.id_uuid)
    assert payload["matches"]["upcoming"]
    assert payload["matches"]["recent"]
    assert payload["stats"]["general"] is not None
    assert payload["roster"][0]["username"] == player.user.username
    assert payload["meta"]["season_id"] == str(season.id_uuid)
    assert payload["meta"]["season_name"] == season.name
    assert len(payload["seasons"]) == 2  # noqa: PLR2004
    assert any(option["is_current"] for option in payload["seasons"])

    legacy_response = client.get(
        f"/api/team/teams/{team.id_uuid}/overview/",
        data={"season": previous_season.id_uuid},
    )

    assert legacy_response.status_code == HTTPStatus.OK
    legacy_payload = legacy_response.json()
    assert legacy_payload["meta"]["season_id"] == str(previous_season.id_uuid)
    assert legacy_payload["matches"]["upcoming"] == []
    assert legacy_payload["matches"]["recent"][0]["competition"] == previous_season.name
    assert legacy_payload["roster"][0]["username"] == player.user.username
