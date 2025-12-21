"""Permission regression tests for PlayerViewSet CRUD operations."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
import pytest

from apps.player.models.player import Player


MODIFY_PERMISSION_DENIED_DETAIL = "You do not have permission to modify this player"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_patch_requires_auth(client: Client) -> None:
    """PATCH on Player detail is not allowed for anonymous users."""
    user = get_user_model().objects.create_user(
        username="player_patch_requires_auth",
        password="pass1234",  # noqa: S106  # nosec
    )

    response = client.patch(
        f"/api/player/players/{user.player.id_uuid}/",
        data=json.dumps({"stats_visibility": "club"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_patch_denies_non_owner(client: Client) -> None:
    """Only the owner (or staff) may update a Player."""
    owner = get_user_model().objects.create_user(
        username="player_patch_owner",
        password="pass1234",  # noqa: S106  # nosec
    )
    other = get_user_model().objects.create_user(
        username="player_patch_other",
        password="pass1234",  # noqa: S106  # nosec
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
        password="pass1234",  # noqa: S106  # nosec
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
def test_player_patch_allows_staff(client: Client) -> None:
    """Staff may update another user's Player resource."""
    owner = get_user_model().objects.create_user(
        username="player_patch_staff_target",
        password="pass1234",  # noqa: S106  # nosec
    )
    staff = get_user_model().objects.create_user(
        username="player_patch_staff_actor",
        password="pass1234",  # noqa: S106  # nosec
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
        password="pass1234",  # noqa: S106  # nosec
    )
    other = get_user_model().objects.create_user(
        username="player_delete_other",
        password="pass1234",  # noqa: S106  # nosec
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
        password="pass1234",  # noqa: S106  # nosec
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
        password="pass1234",  # noqa: S106  # nosec
    )
    staff = get_user_model().objects.create_user(
        username="player_delete_staff_actor",
        password="pass1234",  # noqa: S106  # nosec
        is_staff=True,
    )
    player_id = owner.player.id_uuid
    client.force_login(staff)

    response = client.delete(f"/api/player/players/{player_id}/")

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not Player.objects.filter(id_uuid=player_id).exists()
