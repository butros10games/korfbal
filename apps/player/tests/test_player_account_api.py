"""Current-player account command API regressions."""

from __future__ import annotations

from http import HTTPStatus
import json

from bg_auth.services.reauthentication import issue_reauthentication_token
from django.contrib.auth import get_user_model
from django.core import mail
from django.http import HttpResponse
from django.test import Client, override_settings
from django.utils import timezone
import pytest

from apps.kwt_common.tests.api_test_support import assert_api_error
from apps.player.models import Player
from apps.player.models.push_subscription import PlayerPushSubscription


CURRENT_PASSWORD = "Current-pass-123"  # nosec
NEW_PASSWORD = "New-password-456"  # nosec


def _patch_account(client: Client, **payload: str) -> HttpResponse:
    return client.patch(
        "/api/player/me/",
        data=json.dumps(payload),
        content_type="application/json",
    )


def _delete_account(
    client: Client,
    query: str = "",
    password: str = CURRENT_PASSWORD,
    reauthentication_token: str = "",
    **headers: str,
) -> HttpResponse:
    body = {"current_password": password}
    if reauthentication_token:
        body["reauthentication_token"] = reauthentication_token
    return client.delete(
        f"/api/player/me/account/{query}",
        data=json.dumps(body),
        content_type="application/json",
        **headers,
    )


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_current_player_patch_updates_username_directly(client: Client) -> None:
    """Username changes apply immediately and need no password."""
    user = get_user_model().objects.create_user(
        username="account-before",
        email="before@example.test",
        password=CURRENT_PASSWORD,
    )
    client.force_login(user)

    response = _patch_account(
        client, username="account-after", email="BEFORE@example.test"
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["pending_email"] is None
    user.refresh_from_db()
    assert user.username == "account-after"
    assert user.email == "before@example.test"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
@pytest.mark.parametrize("password", [None, "wrong-password"])
def test_email_change_requires_current_password(
    client: Client, password: str | None
) -> None:
    """A borrowed session cannot redirect password resets and email 2FA."""
    user = get_user_model().objects.create_user(
        username="email-owner", email="owner@example.test", password=CURRENT_PASSWORD
    )
    client.force_login(user)
    extra = {} if password is None else {"current_password": password}

    response = _patch_account(
        client, username="email-owner", email="attacker@example.test", **extra
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "current_password" in response.json()
    user.refresh_from_db()
    assert user.email == "owner@example.test"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_email_change_waits_for_confirmation_from_new_mailbox(client: Client) -> None:
    """The new address gets a link and the old one a warning; nothing moves yet."""
    user = get_user_model().objects.create_user(
        username="changer", email="old@example.test", password=CURRENT_PASSWORD
    )
    client.force_login(user)
    mail.outbox.clear()

    response = _patch_account(
        client,
        username="changer",
        email="new@example.test",
        current_password=CURRENT_PASSWORD,
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["pending_email"] == "new@example.test"
    assert response.json()["user"]["email"] == "old@example.test"
    user.refresh_from_db()
    assert user.email == "old@example.test"
    recipients = sorted(message.to[0] for message in mail.outbox)
    assert recipients == ["new@example.test", "old@example.test"]
    confirmation = next(m for m in mail.outbox if m.to == ["new@example.test"])
    token = confirmation.body.split("/account/confirm-email/", 1)[1].split()[0]

    confirmed = Client().post(
        "/api/auth/email-change/confirm/",
        data=json.dumps({"token": token}),
        content_type="application/json",
    )

    assert confirmed.status_code == HTTPStatus.OK
    user.refresh_from_db()
    assert user.email == "new@example.test"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
@pytest.mark.parametrize(
    "payload",
    [
        {"username": "VICTIM"},
        {"username": "victim@example.test"},
        {"email": "Victim@Example.test", "current_password": CURRENT_PASSWORD},
    ],
)
def test_account_cannot_copy_another_accounts_sign_in_identifier(
    client: Client, payload: dict[str, str]
) -> None:
    """Duplicated identifiers would make the victim's sign-in ambiguous."""
    get_user_model().objects.create_user(
        username="victim", email="victim@example.test", password=CURRENT_PASSWORD
    )
    attacker = get_user_model().objects.create_user(
        username="attacker", email="attacker@example.test", password=CURRENT_PASSWORD
    )
    client.force_login(attacker)

    response = _patch_account(
        client,
        **{"username": "attacker", "email": "attacker@example.test", **payload},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    login = Client().post(
        "/api/auth/jwt/login/",
        data=json.dumps({
            "username": "victim@example.test",
            "password": CURRENT_PASSWORD,
        }),
        content_type="application/json",
    )
    assert login.status_code == HTTPStatus.OK


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
@pytest.mark.parametrize("password", ["", "wrong-password"])
def test_delete_account_requires_current_password(
    client: Client, password: str
) -> None:
    """Deleting the login is irreversible, so a borrowed session is not enough."""
    user = get_user_model().objects.create_user(
        username="keep-me", password=CURRENT_PASSWORD
    )
    client.force_login(user)

    response = _delete_account(client, password=password)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert get_user_model().objects.filter(pk=user.pk).exists()


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
    user = get_user_model().objects.create_user(
        username="delete-account", password=CURRENT_PASSWORD
    )
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

    response = _delete_account(client, f"?user_id={other_user.pk}")

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
    user = get_user_model().objects.create_user(
        username="csrf-deletion", password=CURRENT_PASSWORD
    )
    user_id = user.pk
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = _delete_account(client)

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.headers["Content-Type"].startswith("application/json")
    assert get_user_model().objects.filter(pk=user_id).exists()
    csrf_token = client.get("/api/auth/session/").json()["csrfToken"]
    response = _delete_account(client, HTTP_X_CSRFTOKEN=csrf_token)
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
    user = get_user_model().objects.create_user(
        username="no-player", password=CURRENT_PASSWORD
    )
    user_id = user.pk
    Player.all_objects.filter(user=user).delete()
    client.force_login(user)

    assert _delete_account(client).status_code == HTTPStatus.NO_CONTENT
    assert not get_user_model().objects.filter(pk=user_id).exists()


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_passkey_confirmation_replaces_password_for_sensitive_changes(
    client: Client,
) -> None:
    """A fresh passkey token authorizes one email change and one deletion."""
    user = get_user_model().objects.create_user(
        username="passkey-owner", email="pk@example.test", password=CURRENT_PASSWORD
    )
    client.force_login(user)
    token = issue_reauthentication_token(user)

    changed = _patch_account(
        client,
        username="passkey-owner",
        email="pk-new@example.test",
        reauthentication_token=token,
    )
    replayed = _delete_account(client, password="", reauthentication_token=token)

    assert changed.status_code == HTTPStatus.OK
    assert changed.json()["pending_email"] == "pk-new@example.test"
    assert replayed.status_code == HTTPStatus.BAD_REQUEST
    fresh = _delete_account(
        client, password="", reauthentication_token=issue_reauthentication_token(user)
    )
    assert fresh.status_code == HTTPStatus.NO_CONTENT
