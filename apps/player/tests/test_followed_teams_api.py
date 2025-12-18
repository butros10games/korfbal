"""Tests for player followed-teams API endpoints."""

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test.client import Client
import pytest

from apps.club.models import Club
from apps.team.models import Team


@pytest.mark.django_db
def test_current_player_followed_teams_returns_followed_teams(client: Client) -> None:
    """/api/player/me/followed-teams/ returns only teams followed by the user."""
    club = Club.objects.create(name="Follow Club")
    team_a = Team.objects.create(name="A", club=club)
    team_b = Team.objects.create(name="B", club=club)

    user = get_user_model().objects.create_user(
        username="follow_user",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player
    player.team_follow.add(team_a)

    client.force_login(user)

    response = client.get("/api/player/me/followed-teams/", secure=True)
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert isinstance(payload, list)
    assert [row["id_uuid"] for row in payload] == [str(team_a.id_uuid)]
    assert payload[0]["name"] == team_a.name
    assert payload[0]["club"]["id_uuid"] == str(club.id_uuid)

    # Sanity: non-followed team is not returned
    assert str(team_b.id_uuid) not in {row["id_uuid"] for row in payload}


@pytest.mark.django_db
def test_player_followed_teams_detail_endpoint_allows_self(client: Client) -> None:
    """/api/player/players/<uuid>/followed-teams/ works when requesting self."""
    club = Club.objects.create(name="Follow Club 2")
    team = Team.objects.create(name="A", club=club)

    user = get_user_model().objects.create_user(
        username="follow_user_2",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player
    player.team_follow.add(team)

    client.force_login(user)

    response = client.get(
        f"/api/player/players/{player.id_uuid}/followed-teams/",
        secure=True,
    )
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert [row["id_uuid"] for row in payload] == [str(team.id_uuid)]
