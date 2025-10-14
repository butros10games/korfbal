"""Tests for hub views and APIs."""

from datetime import timedelta
import json

from django.contrib.auth import get_user_model
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, Shot
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


TEST_PASSWORD = "pass1234"  # noqa: S105  # nosec B105 - test credential constant
HTTP_STATUS_OK = 200
EXPECTED_HOME_SCORE = 2
EXPECTED_AWAY_SCORE = 1


@pytest.mark.django_db
def test_hub_index_allows_authenticated_spectator(client: Client) -> None:
    """Ensure hub index loads for users without a Player profile."""
    user = get_user_model().objects.create_user(
        username="spectator",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.get(reverse("index"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["match"] is None
    assert response.context["match_data"] is None


@pytest.mark.django_db
def test_hub_index_player_without_teams(client: Client) -> None:
    """Test hub index for authenticated user with Player profile but no teams."""
    user = get_user_model().objects.create_user(
        username="player_no_teams",
        password=TEST_PASSWORD,
    )
    # Player is created automatically by signal
    client.force_login(user)

    response = client.get(reverse("index"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["match"] is None
    assert response.context["match_data"] is None
    assert response.context["home_score"] == 0
    assert response.context["away_score"] == 0


@pytest.mark.django_db
def test_hub_index_player_with_teams_no_matches(client: Client) -> None:
    """Test hub index for player with teams but no upcoming matches."""
    user = get_user_model().objects.create_user(
        username="player_with_teams",
        password=TEST_PASSWORD,
    )
    # Player is created automatically by signal
    player = user.player

    # Create a club and team
    club = Club.objects.create(name="Test Club")
    team = Team.objects.create(name="Test Team", club=club)

    # Create season and team data
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    team_data = TeamData.objects.create(team=team, season=season)
    team_data.players.set([player])

    client.force_login(user)

    response = client.get(reverse("index"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["match"] is None
    assert response.context["match_data"] is None
    assert response.context["home_score"] == 0
    assert response.context["away_score"] == 0


@pytest.mark.django_db
def test_hub_index_player_with_upcoming_home_match(client: Client) -> None:
    """Test hub index for player with upcoming home match."""
    user = get_user_model().objects.create_user(
        username="player_home_match",
        password=TEST_PASSWORD,
    )
    # Player is created automatically by signal
    player = user.player

    # Create clubs and teams
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    # Create season and team data
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    team_data = TeamData.objects.create(team=home_team, season=season)
    team_data.players.set([player])

    # Create upcoming match
    future_time = timezone.now() + timedelta(hours=2)
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=future_time,
    )

    client.force_login(user)

    response = client.get(reverse("index"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["match"] == match
    assert "match_data" in response.context
    assert response.context["match_data"].match_link == match
    assert response.context["home_score"] is None
    assert response.context["away_score"] is None


@pytest.mark.django_db
def test_hub_index_player_with_active_match_and_scores(client: Client) -> None:
    """Test hub index for player with active match including scores."""
    user = get_user_model().objects.create_user(
        username="player_active_match",
        password=TEST_PASSWORD,
    )
    # Player is created automatically by signal
    player = user.player

    # Create clubs and teams
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    # Create season and team data
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    team_data = TeamData.objects.create(team=home_team, season=season)
    team_data.players.set([player])

    # Create active match
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.create(match_link=match, status="active")

    # Add some shots/scores
    Shot.objects.create(
        match_data=match_data, team=home_team, player=player, scored=True
    )
    Shot.objects.create(
        match_data=match_data, team=home_team, player=player, scored=True
    )
    Shot.objects.create(
        match_data=match_data, team=away_team, player=player, scored=True
    )
    Shot.objects.create(
        match_data=match_data, team=away_team, player=player, scored=False
    )  # Miss

    client.force_login(user)

    response = client.get(reverse("index"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["match"] == match
    assert response.context["match_data"] == match_data
    assert response.context["home_score"] == EXPECTED_HOME_SCORE  # 2 scored shots
    assert response.context["away_score"] == EXPECTED_AWAY_SCORE  # 1 scored shot


@pytest.mark.django_db
def test_catalog_data_returns_empty_lists_for_spectator(client: Client) -> None:
    """Ensure catalog data API responds gracefully without a Player profile."""
    user = get_user_model().objects.create_user(
        username="spectator2",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.post(
        reverse("api_catalog_data"),
        data=json.dumps({"value": "teams"}),
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert payload["type"] == "teams"
    assert payload["connected"] == []
    assert payload["following"] == []


@pytest.mark.django_db
def test_hub_index_caching_match_scores(client: Client) -> None:
    """Test that match scores are cached properly."""
    user = get_user_model().objects.create_user(
        username="score_cache_user_unique",
        password=TEST_PASSWORD,
    )
    # Player is created automatically by signal
    player = user.player

    # Create clubs and teams
    home_club = Club.objects.create(name="Home Cache Club")
    Club.objects.create(name="Away Cache Club")
    home_team = Team.objects.create(name="Home Cache Team", club=home_club)
    away_team = Team.objects.create(name="Away Cache Team", club=home_club)

    # Create season and team data
    season = Season.objects.create(
        name="Score Cache Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    team_data = TeamData.objects.create(team=home_team, season=season)
    team_data.players.set([player])

    # Create active match
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.create(match_link=match, status="active")

    # Add some shots/scores
    Shot.objects.create(
        match_data=match_data, team=home_team, player=player, scored=True
    )
    Shot.objects.create(
        match_data=match_data, team=home_team, player=player, scored=True
    )

    client.force_login(user)

    response = client.get(reverse("index"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["home_score"] == EXPECTED_HOME_SCORE
    assert response.context["away_score"] == 0
