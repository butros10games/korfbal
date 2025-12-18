"""Tests for the team API endpoints."""

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, Shot
from apps.schedule.models import Match, Season
from apps.team.models import Team
from apps.team.models.team_data import TeamData


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_team_overview_includes_matches_stats_and_roster(  # noqa: PLR0915
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
        password="pass1234",  # noqa: S106  # nosec
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

    future_match_data = MatchData.objects.get(match_link=future_match)
    future_match_data.status = "upcoming"
    future_match_data.save(update_fields=["status"])

    past_match_data = MatchData.objects.get(match_link=past_match)
    past_match_data.status = "finished"
    past_match_data.home_score = 21
    past_match_data.away_score = 18
    past_match_data.save(update_fields=["status", "home_score", "away_score"])

    legacy_match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=previous_season,
        start_time=timezone.now() - timedelta(days=200),
    )
    legacy_match_data = MatchData.objects.get(match_link=legacy_match)
    legacy_match_data.status = "finished"
    legacy_match_data.home_score = 18
    legacy_match_data.away_score = 16
    legacy_match_data.save(update_fields=["status", "home_score", "away_score"])

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

    # Guest players who scored should appear in stats and roster
    guest_user = get_user_model().objects.create_user(
        username="guest_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    guest_player = guest_user.player
    past_match_data.players.create(player=guest_player, team=team)
    Shot.objects.create(
        match_data=past_match_data,
        player=guest_player,
        team=team,
        for_team=True,
        scored=True,
    )

    # Players that only show up in shot data should still appear in roster/stats
    shot_only_user = get_user_model().objects.create_user(
        username="shot_only_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    shot_only_player = shot_only_user.player
    Shot.objects.create(
        match_data=past_match_data,
        player=shot_only_player,
        team=team,
        for_team=False,
        scored=False,
    )

    response_with_guest = client.get(f"/api/team/teams/{team.id_uuid}/overview/")
    assert response_with_guest.status_code == HTTPStatus.OK
    stats_payload = response_with_guest.json()["stats"]["players"]
    assert any(line["username"] == guest_player.user.username for line in stats_payload)
    assert any(
        line["username"] == shot_only_player.user.username for line in stats_payload
    )
    roster_payload = response_with_guest.json()["roster"]
    assert any(
        line["username"] == guest_player.user.username for line in roster_payload
    )
    assert any(
        line["username"] == shot_only_player.user.username for line in roster_payload
    )

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


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_team_overview_can_skip_stats_and_roster(client: Client) -> None:
    """The overview endpoint should support a lightweight mode for faster loads."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )

    club = Club.objects.create(name="Team Club")
    opponent_club = Club.objects.create(name="Opponent Club")
    team = Team.objects.create(name="Team 1", club=club)
    opponent_team = Team.objects.create(name="Opponent 1", club=opponent_club)

    user = get_user_model().objects.create_user(
        username="player",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    team_data = TeamData.objects.create(team=team, season=season)
    team_data.players.add(player)

    match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() + timedelta(days=3),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "upcoming"
    match_data.save(update_fields=["status"])

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/overview/",
        data={"include_stats": "0", "include_roster": "0"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["team"]["id_uuid"] == str(team.id_uuid)
    assert payload["matches"]["upcoming"]
    assert payload["stats"]["general"] is None
    assert payload["stats"]["players"] == []
    assert payload["roster"] == []
