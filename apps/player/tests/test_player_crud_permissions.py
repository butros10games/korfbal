"""Permission regression tests for PlayerViewSet CRUD operations."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
import pytest

from apps.club.models import Club
from apps.player.models.player import Player
from apps.team.models import Team


MODIFY_PERMISSION_DENIED_DETAIL = "You do not have permission to modify this player"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_create_route_is_not_exposed(client: Client) -> None:
    """Player creation remains owned by the user lifecycle signal."""
    user = get_user_model().objects.create_user(
        username="player_create_not_exposed",
        password="pass1234",  # nosec
    )
    client.force_login(user)
    before = Player.objects.count()

    response = client.post(
        "/api/player/players/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert Player.objects.count() == before


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_patch_requires_auth(client: Client) -> None:
    """PATCH on Player detail is not allowed for anonymous users."""
    user = get_user_model().objects.create_user(
        username="player_patch_requires_auth",
        password="pass1234",  # nosec
    )

    response = client.patch(
        f"/api/player/players/{user.player.id_uuid}/",
        data=json.dumps({"stats_visibility": "club"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["Content-Type"].startswith("application/json")


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_patch_denies_non_owner(client: Client) -> None:
    """Only the owner (or staff) may update a Player."""
    owner = get_user_model().objects.create_user(
        username="player_patch_owner",
        password="pass1234",  # nosec
    )
    other = get_user_model().objects.create_user(
        username="player_patch_other",
        password="pass1234",  # nosec
    )
    client.force_login(other)

    response = client.patch(
        f"/api/player/players/{owner.player.id_uuid}/",
        data=json.dumps({"stats_visibility": "club"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["detail"] == MODIFY_PERMISSION_DENIED_DETAIL


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_patch_allows_owner(client: Client) -> None:
    """A user may update their own Player resource."""
    owner = get_user_model().objects.create_user(
        username="player_patch_allows_owner",
        password="pass1234",  # nosec
    )
    client.force_login(owner)

    response = client.patch(
        f"/api/player/players/{owner.player.id_uuid}/",
        data=json.dumps({"stats_visibility": "club"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK

    owner.refresh_from_db()
    assert owner.player.stats_visibility == Player.Visibility.CLUB


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_patch_persists_follow_relationships(client: Client) -> None:
    """Profile commands preserve writable many-to-many fields."""
    owner = get_user_model().objects.create_user(
        username="player_patch_follows",
        password="pass1234",  # nosec
    )
    club = Club.objects.create(name="Profile command club")
    team = Team.objects.create(name="Profile command team", club=club)
    client.force_login(owner)

    response = client.patch(
        f"/api/player/players/{owner.player.id_uuid}/",
        data=json.dumps({
            "club_follow": [str(club.id_uuid)],
            "team_follow": [str(team.id_uuid)],
        }),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["club_follow"] == [str(club.id_uuid)]
    assert response.json()["team_follow"] == [str(team.id_uuid)]
    assert list(owner.player.club_follow.values_list("id_uuid", flat=True)) == [
        club.id_uuid
    ]
    assert list(owner.player.team_follow.values_list("id_uuid", flat=True)) == [
        team.id_uuid
    ]


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_patch_allows_staff(client: Client) -> None:
    """Staff may update another user's Player resource."""
    owner = get_user_model().objects.create_user(
        username="player_patch_staff_target",
        password="pass1234",  # nosec
    )
    staff = get_user_model().objects.create_user(
        username="player_patch_staff_actor",
        password="pass1234",  # nosec
        is_staff=True,
    )
    client.force_login(staff)

    response = client.patch(
        f"/api/player/players/{owner.player.id_uuid}/",
        data=json.dumps({"profile_picture_visibility": "club"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK

    owner.refresh_from_db()
    assert owner.player.profile_picture_visibility == Player.Visibility.CLUB


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_delete_denies_non_owner(client: Client) -> None:
    """Only the owner (or staff) may delete a Player."""
    owner = get_user_model().objects.create_user(
        username="player_delete_owner",
        password="pass1234",  # nosec
    )
    other = get_user_model().objects.create_user(
        username="player_delete_other",
        password="pass1234",  # nosec
    )
    client.force_login(other)

    response = client.delete(f"/api/player/players/{owner.player.id_uuid}/")

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert Player.objects.filter(id_uuid=owner.player.id_uuid).exists()


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_delete_allows_owner(client: Client) -> None:
    """Owners may delete their own Player resource."""
    owner = get_user_model().objects.create_user(
        username="player_delete_allows_owner",
        password="pass1234",  # nosec
    )
    player_id = owner.player.id_uuid
    user_id = owner.id
    client.force_login(owner)

    response = client.delete(f"/api/player/players/{player_id}/")

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not Player.objects.filter(id_uuid=player_id).exists()
    assert get_user_model().objects.filter(id=user_id).exists()


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_delete_allows_staff(client: Client) -> None:
    """Staff may delete another user's Player resource."""
    owner = get_user_model().objects.create_user(
        username="player_delete_staff_target",
        password="pass1234",  # nosec
    )
    staff = get_user_model().objects.create_user(
        username="player_delete_staff_actor",
        password="pass1234",  # nosec
        is_staff=True,
    )
    player_id = owner.player.id_uuid
    client.force_login(staff)

    response = client.delete(f"/api/player/players/{player_id}/")

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not Player.objects.filter(id_uuid=player_id).exists()
