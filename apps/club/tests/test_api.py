"""Tests for the club API endpoints."""

from datetime import timedelta
from http import HTTPStatus

from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_overview_returns_team_and_match_payload(client: Client) -> None:
    """The overview endpoint should include teams plus upcoming and recent matches."""
    season = Season.objects.create(
        name="2025/2026",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    club = Club.objects.create(name="Test Club")
    opponent_club = Club.objects.create(name="Opponent Club")

    team = Team.objects.create(name="Test Team", club=club)
    opponent_team = Team.objects.create(name="Opponent Team", club=opponent_club)

    future_match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() + timedelta(days=2),
    )
    past_match = Match.objects.create(
        home_team=opponent_team,
        away_team=team,
        season=season,
        start_time=timezone.now() - timedelta(days=4),
    )

    future_match_data = MatchData.objects.get(match_link=future_match)
    future_match_data.status = "upcoming"
    future_match_data.save(update_fields=["status"])

    past_match_data = MatchData.objects.get(match_link=past_match)
    past_match_data.status = "finished"
    past_match_data.home_score = 15
    past_match_data.away_score = 14
    past_match_data.save(update_fields=["status", "home_score", "away_score"])

    response = client.get(f"/api/club/clubs/{club.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["club"]["id_uuid"] == str(club.id_uuid)
    assert len(payload["teams"]) == 1
    assert payload["teams"][0]["name"] == team.name
    assert payload["matches"]["upcoming"][0]["status"] == "upcoming"
    assert payload["matches"]["recent"][0]["status"] == "finished"
    assert payload["meta"]["season_id"] == str(season.id_uuid)
    assert payload["meta"]["season_name"] == season.name
    assert payload["seasons"]
    assert payload["seasons"][0]["id_uuid"] == str(season.id_uuid)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_overview_can_filter_by_season(client: Client) -> None:
    """Explicit season query should scope teams and matches."""
    today = timezone.now().date()
    current_season = Season.objects.create(
        name="2025/2026",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=335),
    )
    previous_season = Season.objects.create(
        name="2024/2025",
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=35),
    )

    club = Club.objects.create(name="Filter Club")
    opponent_club = Club.objects.create(name="Filter Opponent")

    recent_team = Team.objects.create(name="Recent Team", club=club)
    legacy_team = Team.objects.create(name="Legacy Team", club=club)
    opponent_team = Team.objects.create(name="Opponent Team", club=opponent_club)

    TeamData.objects.create(team=recent_team, season=current_season)
    TeamData.objects.create(team=legacy_team, season=previous_season)

    recent_match = Match.objects.create(
        home_team=recent_team,
        away_team=opponent_team,
        season=current_season,
        start_time=timezone.now() + timedelta(days=3),
    )
    legacy_match = Match.objects.create(
        home_team=opponent_team,
        away_team=legacy_team,
        season=previous_season,
        start_time=timezone.now() - timedelta(days=10),
    )

    recent_data = MatchData.objects.get(match_link=recent_match)
    recent_data.status = "upcoming"
    recent_data.save(update_fields=["status"])

    legacy_data = MatchData.objects.get(match_link=legacy_match)
    legacy_data.status = "finished"
    legacy_data.home_score = 18
    legacy_data.away_score = 14
    legacy_data.save(update_fields=["status", "home_score", "away_score"])

    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/overview/",
        {"season": str(previous_season.id_uuid)},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["meta"]["season_id"] == str(previous_season.id_uuid)
    assert payload["meta"]["season_name"] == previous_season.name
    assert [team["name"] for team in payload["teams"]] == ["Legacy Team"]
    assert payload["matches"]["upcoming"] == []
    assert payload["matches"]["recent"][0]["status"] == "finished"
    assert payload["matches"]["recent"][0]["competition"] == previous_season.name
