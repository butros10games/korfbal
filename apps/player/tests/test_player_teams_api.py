"""Tests for player teams endpoint (/teams/)."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.player.models import Player
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


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_current_player_teams_returns_playing_and_following(client: Client) -> None:
    """The current player receives playing/coaching/following team groups."""
    season = _create_current_season()
    club = Club.objects.create(name="Teams Club")
    team_playing = Team.objects.create(name="A", club=club)
    team_following = Team.objects.create(name="B", club=club)

    user = get_user_model().objects.create_user(
        username="teams_me",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    team_data = TeamData.objects.create(
        team=team_playing, season=season, competition=""
    )
    team_data.players.add(player)

    team_coach = Team.objects.create(name="Coach", club=club)
    coach_data = TeamData.objects.create(
        team=team_coach,
        season=season,
        competition="",
    )
    coach_data.coach.add(player)

    player.team_follow.add(team_following)

    client.force_login(user)

    response = client.get("/api/player/me/teams/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert set(payload.keys()) == {"playing", "coaching", "following"}
    assert [row["id_uuid"] for row in payload["playing"]] == [str(team_playing.id_uuid)]
    assert [row["id_uuid"] for row in payload["coaching"]] == [str(team_coach.id_uuid)]
    assert [row["id_uuid"] for row in payload["following"]] == [
        str(team_following.id_uuid)
    ]


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_other_player_teams_club_blocks_anonymous(client: Client) -> None:
    """Anonymous viewers cannot see club-visible teams for another player."""
    season = _create_current_season()
    club = Club.objects.create(name="Private Teams Club")
    team = Team.objects.create(name="A", club=club)

    target_user = get_user_model().objects.create_user(
        username="teams_private_target",
        password="pass1234",  # noqa: S106  # nosec
    )
    target_player = target_user.player
    target_player.teams_visibility = Player.Visibility.CLUB
    target_player.save(update_fields=["teams_visibility"])

    team_data = TeamData.objects.create(team=team, season=season, competition="")
    team_data.players.add(target_player)

    response = client.get(f"/api/player/players/{target_player.id_uuid}/teams/")
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == PRIVATE_ACCOUNT_DETAIL


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_other_player_teams_club_allows_connected(client: Client) -> None:
    """Connected club members can see club-visible teams for another player."""
    season = _create_current_season()
    club = Club.objects.create(name="Connected Teams Club")
    team = Team.objects.create(name="A", club=club)

    viewer_user = get_user_model().objects.create_user(
        username="teams_private_viewer",
        password="pass1234",  # noqa: S106  # nosec
    )
    viewer_player = viewer_user.player

    target_user = get_user_model().objects.create_user(
        username="teams_private_target_2",
        password="pass1234",  # noqa: S106  # nosec
    )
    target_player = target_user.player
    target_player.teams_visibility = Player.Visibility.CLUB
    target_player.save(update_fields=["teams_visibility"])

    team_data = TeamData.objects.create(team=team, season=season, competition="")
    team_data.players.add(viewer_player, target_player)

    client.force_login(viewer_user)

    response = client.get(f"/api/player/players/{target_player.id_uuid}/teams/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert set(payload.keys()) == {"playing", "coaching", "following"}
    assert [row["id_uuid"] for row in payload["playing"]] == [str(team.id_uuid)]
    assert payload["coaching"] == []
