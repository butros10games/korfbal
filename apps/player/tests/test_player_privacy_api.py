"""Tests for player privacy/visibility behaviour."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Season
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


PRIVATE_ACCOUNT_DETAIL = {"code": "private_account", "detail": "Private account"}


def _create_current_season() -> Season:
    today = timezone.now().date()
    return Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )


def _connect_players_in_same_club(
    *,
    club: Club,
    season: Season,
    players: list[Player],
) -> None:
    team = Team.objects.create(name="Team A", club=club)
    team_data = TeamData.objects.create(team=team, season=season, competition="")
    team_data.players.add(*players)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_other_player_stats_club_blocks_anonymous(client: Client) -> None:
    """Anonymous users cannot access club-only stats."""
    target_user = get_user_model().objects.create_user(
        username="club_target_anon",
        password="pass1234",  # noqa: S106  # nosec
    )
    target_player = target_user.player
    target_player.stats_visibility = Player.Visibility.CLUB
    target_player.save(update_fields=["stats_visibility"])

    response = client.get(f"/api/player/players/{target_player.id_uuid}/stats/")
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == PRIVATE_ACCOUNT_DETAIL


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_other_player_stats_club_allows_connected_and_blocks_unconnected(
    client: Client,
) -> None:
    """Club-connected viewers can access stats; unconnected viewers cannot."""
    season = _create_current_season()
    club = Club.objects.create(name="Stats Club")

    viewer_user = get_user_model().objects.create_user(
        username="club_viewer",
        password="pass1234",  # noqa: S106  # nosec
    )
    viewer_player = viewer_user.player

    target_user = get_user_model().objects.create_user(
        username="club_target",
        password="pass1234",  # noqa: S106  # nosec
    )
    target_player = target_user.player
    target_player.stats_visibility = Player.Visibility.CLUB
    target_player.save(update_fields=["stats_visibility"])

    _connect_players_in_same_club(
        club=club,
        season=season,
        players=[viewer_player, target_player],
    )

    client.force_login(viewer_user)
    response_ok = client.get(f"/api/player/players/{target_player.id_uuid}/stats/")
    assert response_ok.status_code == HTTPStatus.OK
    assert response_ok.json() == {
        "shots_for": 0,
        "shots_against": 0,
        "goals_for": 0,
        "goals_against": 0,
        "mvps": 0,
        "mvp_matches": [],
        "goal_types": {"for": [], "against": []},
    }

    other_user = get_user_model().objects.create_user(
        username="outsider",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(other_user)
    response_forbidden = client.get(
        f"/api/player/players/{target_player.id_uuid}/stats/"
    )
    assert response_forbidden.status_code == HTTPStatus.FORBIDDEN
    assert response_forbidden.json() == PRIVATE_ACCOUNT_DETAIL


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_other_player_stats_club_allows_connected_via_membership(
    client: Client,
) -> None:
    """Club-connected viewers can access club-only stats via membership history."""
    club = Club.objects.create(name="Membership Club")

    viewer_user = get_user_model().objects.create_user(
        username="member_viewer",
        password="pass1234",  # noqa: S106  # nosec
    )
    target_user = get_user_model().objects.create_user(
        username="member_target",
        password="pass1234",  # noqa: S106  # nosec
    )

    target_player = target_user.player
    target_player.stats_visibility = Player.Visibility.CLUB
    target_player.save(update_fields=["stats_visibility"])

    PlayerClubMembership.objects.create(player=viewer_user.player, club=club)
    PlayerClubMembership.objects.create(player=target_player, club=club)

    client.force_login(viewer_user)
    response_ok = client.get(f"/api/player/players/{target_player.id_uuid}/stats/")
    assert response_ok.status_code == HTTPStatus.OK


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_other_player_overview_club_allows_connected_and_blocks_unconnected(
    client: Client,
) -> None:
    """Club-connected viewers can access overview; unconnected viewers cannot."""
    season = _create_current_season()
    club = Club.objects.create(name="Overview Club")

    viewer_user = get_user_model().objects.create_user(
        username="overview_viewer",
        password="pass1234",  # noqa: S106  # nosec
    )
    viewer_player = viewer_user.player

    target_user = get_user_model().objects.create_user(
        username="overview_target",
        password="pass1234",  # noqa: S106  # nosec
    )
    target_player = target_user.player
    target_player.stats_visibility = Player.Visibility.CLUB
    target_player.save(update_fields=["stats_visibility"])

    _connect_players_in_same_club(
        club=club,
        season=season,
        players=[viewer_player, target_player],
    )

    client.force_login(viewer_user)
    response_ok = client.get(f"/api/player/players/{target_player.id_uuid}/overview/")
    assert response_ok.status_code == HTTPStatus.OK

    payload = response_ok.json()
    assert set(payload.keys()) >= {"matches", "seasons", "meta"}
    assert set(payload["matches"].keys()) >= {"upcoming", "recent"}

    other_user = get_user_model().objects.create_user(
        username="overview_outsider",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(other_user)
    response_forbidden = client.get(
        f"/api/player/players/{target_player.id_uuid}/overview/"
    )
    assert response_forbidden.status_code == HTTPStatus.FORBIDDEN
    assert response_forbidden.json() == PRIVATE_ACCOUNT_DETAIL


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_privacy_settings_endpoint_get_and_patch(client: Client) -> None:
    """Players can read and update their privacy visibility settings."""
    user = get_user_model().objects.create_user(
        username="privacy_me",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response_get = client.get("/api/player/me/privacy-settings/")
    assert response_get.status_code == HTTPStatus.OK
    assert response_get.json() == {
        "profile_picture_visibility": Player.Visibility.PUBLIC,
        "stats_visibility": Player.Visibility.PUBLIC,
        "teams_visibility": Player.Visibility.PUBLIC,
    }

    response_patch = client.patch(
        "/api/player/me/privacy-settings/",
        data=json.dumps({"stats_visibility": Player.Visibility.PRIVATE}),
        content_type="application/json",
    )
    assert response_patch.status_code == HTTPStatus.OK
    payload = response_patch.json()
    # 'private' is coerced to 'club' (deprecated option).
    assert payload["stats_visibility"] == Player.Visibility.CLUB
    assert payload["can_view_stats"] is True

    response_patch_teams = client.patch(
        "/api/player/me/privacy-settings/",
        data=json.dumps({"teams_visibility": Player.Visibility.PRIVATE}),
        content_type="application/json",
    )
    assert response_patch_teams.status_code == HTTPStatus.OK
    payload_teams = response_patch_teams.json()
    assert payload_teams["teams_visibility"] == Player.Visibility.CLUB
    assert payload_teams["can_view_teams"] is True
