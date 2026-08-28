"""Current-player account command API regressions."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
import pytest


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
    assert response.json() == {
        "current_password": ["The current password is incorrect."]
    }
