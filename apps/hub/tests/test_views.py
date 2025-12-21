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
    payload = response.json()
    assert payload["match"] is None
    assert payload["match_data"] is None
    assert payload["home_score"] == 0
    assert payload["away_score"] == 0


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
    payload = response.json()
    assert payload["match"] is None
    assert payload["match_data"] is None
    assert payload["home_score"] == 0
    assert payload["away_score"] == 0


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
    payload = response.json()
    assert payload["match"] is None
    assert payload["match_data"] is None
    assert payload["home_score"] == 0
    assert payload["away_score"] == 0


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
    payload = response.json()
    assert payload["match"]["id_uuid"] == str(match.id_uuid)
    assert payload["match_data"] is not None
    assert payload["home_score"] is None
    assert payload["away_score"] is None


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
    payload = response.json()
    assert payload["match"]["id_uuid"] == str(match.id_uuid)
    assert payload["match_data"]["id_uuid"] == str(match_data.id_uuid)
    assert payload["home_score"] == EXPECTED_HOME_SCORE  # 2 scored shots
    assert payload["away_score"] == EXPECTED_AWAY_SCORE  # 1 scored shot


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
    payload = response.json()
    assert payload["home_score"] == EXPECTED_HOME_SCORE
    assert payload["away_score"] == 0


@pytest.mark.django_db
def test_hub_updates_returns_recent_matches(client: Client) -> None:
    """The update feed should list recent matches in reverse start-time order."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025 Season",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=335),
    )

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    newest_match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(hours=1),
    )
    older_match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(days=2),
    )

    response = client.get(reverse("hub-updates"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2  # noqa: PLR2004
    assert payload[0]["id"] == str(newest_match.id_uuid)
    assert payload[1]["id"] == str(older_match.id_uuid)
    assert "Home Club" in payload[0]["title"]
    assert "Away Club" in payload[0]["title"]
    assert season.name in payload[0]["description"]


@pytest.mark.django_db
def test_hub_updates_falls_back_when_no_recent_matches(client: Client) -> None:
    """When there are no matches in the last 7 days, fall back to latest 5."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025 Season",
        start_date=today - timedelta(days=400),
        end_date=today + timedelta(days=335),
    )

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    old_match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(days=30),
    )

    response = client.get(reverse("hub-updates"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == str(old_match.id_uuid)


@pytest.mark.django_db
def test_catalog_data_ignores_non_dict_json_payload(client: Client) -> None:
    """The catalog_data endpoint should ignore array/invalid JSON bodies."""
    user = get_user_model().objects.create_user(
        username="catalog_non_dict",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.post(
        reverse("api_catalog_data"),
        data=json.dumps(["teams"]),
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert not payload["type"]
    assert payload["connected"] == []
    assert payload["following"] == []
