"""Current-player account command API regressions."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
import pytest

from apps.kwt_common.tests.api_test_support import assert_api_error
from apps.player.models import Player
from apps.player.models.push_subscription import PlayerPushSubscription


CURRENT_PASSWORD = "Current-pass-123"  # nosec
NEW_PASSWORD = "New-password-456"  # nosec


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_current_player_patch_updates_account_fields(client: Client) -> None:
    """The frontend account command has a real authenticated API route."""
    user = get_user_model().objects.create_user(
        username="account-before",
        email="before@example.test",
        password=CURRENT_PASSWORD,
    )
    client.force_login(user)

    response = client.patch(
        "/api/player/me/",
        data=json.dumps({
            "username": "account-after",
            "email": "after@example.test",
        }),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["user"] == {
        "id": user.pk,
        "username": "account-after",
        "email": "after@example.test",
        "first_name": "",
        "last_name": "",
    }
    user.refresh_from_db()
    assert user.username == "account-after"
    assert user.email == "after@example.test"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_current_player_patch_requires_authentication(client: Client) -> None:
    """Anonymous account writes return JSON authorization errors."""
    response = client.patch(
        "/api/player/me/",
        data=json.dumps({
            "username": "anonymous",
            "email": "anonymous@example.test",
        }),
        content_type="application/json",
    )

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    assert response.headers["Content-Type"].startswith("application/json")


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_current_player_password_change_keeps_session_authenticated(
    client: Client,
) -> None:
    """Changing a password updates credentials without logging out the web client."""
    user = get_user_model().objects.create_user(
        username="password-player",
        password=CURRENT_PASSWORD,
    )
    client.force_login(user)

    response = client.post(
        "/api/player/me/password/",
        data=json.dumps({
            "current_password": CURRENT_PASSWORD,
            "new_password1": NEW_PASSWORD,
            "new_password2": NEW_PASSWORD,
        }),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert client.get("/api/player/me/").status_code == HTTPStatus.OK


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_current_player_password_change_returns_field_errors(client: Client) -> None:
    """Incorrect credentials retain DRF's structured validation payload."""
    user = get_user_model().objects.create_user(
        username="wrong-current-password",
        password=CURRENT_PASSWORD,
    )
    client.force_login(user)

    response = client.post(
        "/api/player/me/password/",
        data=json.dumps({
            "current_password": "wrong-password",
            "new_password1": NEW_PASSWORD,
            "new_password2": NEW_PASSWORD,
        }),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert_api_error(
        response.json(), {"current_password": ["The current password is incorrect."]}
    )


@pytest.mark.django_db
@pytest.mark.parametrize("imported", [False, True])
@override_settings(SECURE_SSL_REDIRECT=False)
def test_delete_account_preserves_player_and_ends_sessions(
    client: Client, imported: bool
) -> None:
    """Even a player with no protected history survives account deletion intact."""
    user = get_user_model().objects.create_user(username="delete-account")
    other_user = get_user_model().objects.create_user(username="keep-account")
    player = Player.all_objects.get(user=user)
    player.name = "Synthetic Player"
    if imported:
        player.knkv_person_id = "synthetic-knkv-person"
        player.knkv_privacy = "NORMAL"
        player.knkv_observed_at = timezone.now()
    player.save()
    before = Player.all_objects.values().get(pk=player.pk)
    user_id = user.pk
    subscription = PlayerPushSubscription.objects.create(
        user=user,
        endpoint="https://push.example.test/synthetic",
        subscription={},
    )
    client.force_login(user)
    other_session = Client()
    other_session.force_login(user)

    response = client.delete(f"/api/player/me/account/?user_id={other_user.pk}")

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not response.content
    assert not get_user_model().objects.filter(pk=user_id).exists()
    assert get_user_model().objects.filter(pk=other_user.pk).exists()
    assert Player.all_objects.values().get(pk=player.pk) == before | {"user_id": None}
    assert not PlayerPushSubscription.objects.filter(pk=subscription.pk).exists()
    assert "_auth_user_id" not in client.session
    assert client.get("/api/auth/session/").json()["authenticated"] is False
    assert other_session.get("/api/auth/session/").json()["authenticated"] is False


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_delete_account_requires_authentication(client: Client) -> None:
    """Anonymous deletion never uses the current-player debug fallback."""
    user = get_user_model().objects.create_user(username="keep-anonymous")

    response = client.delete("/api/player/me/account/")

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    assert response.headers["Content-Type"].startswith("application/json")
    assert get_user_model().objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_delete_account_enforces_csrf() -> None:
    """Authenticated browser deletion requires a valid CSRF token."""
    user = get_user_model().objects.create_user(username="csrf-deletion")
    user_id = user.pk
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = client.delete("/api/player/me/account/")

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.headers["Content-Type"].startswith("application/json")
    assert get_user_model().objects.filter(pk=user_id).exists()
    csrf_token = client.get("/api/auth/session/").json()["csrfToken"]
    response = client.delete("/api/player/me/account/", HTTP_X_CSRFTOKEN=csrf_token)
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not get_user_model().objects.filter(pk=user_id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post", "patch"])
@override_settings(SECURE_SSL_REDIRECT=False)
def test_account_deletion_only_accepts_delete(client: Client, method: str) -> None:
    """Reads and other account writes cannot accidentally delete a user."""
    user = get_user_model().objects.create_user(username="wrong-method")
    client.force_login(user)

    response = getattr(client, method)("/api/player/me/account/")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert get_user_model().objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_delete_account_without_player(client: Client) -> None:
    """Account deletion does not depend on an existing or visible player profile."""
    user = get_user_model().objects.create_user(username="no-player")
    user_id = user.pk
    Player.all_objects.filter(user=user).delete()
    client.force_login(user)

    assert client.delete("/api/player/me/account/").status_code == HTTPStatus.NO_CONTENT
    assert not get_user_model().objects.filter(pk=user_id).exists()
