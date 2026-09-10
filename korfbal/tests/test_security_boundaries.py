"""HTTP regressions for alternate authentication and refresh-session boundaries."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import secrets
from threading import Barrier

from bg_auth.jwt import (
    JwtError,
    credentials_are_current,
    decode,
    issue_pair,
    rotate_refresh_token,
)
from bg_auth.models import UserProfile
from bg_auth.services.two_factor import TwoFactorService
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.test import Client
import pytest


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_basic_password_cannot_bypass_mfa(client: Client, prefix: str) -> None:
    """Password-only Basic headers cannot reach authenticated player endpoints."""
    password = secrets.token_urlsafe(24)
    user = User.objects.create_user(username="mfa-player", password=password)
    UserProfile.objects.update_or_create(user=user, defaults={"email_2fa": True})
    credentials = base64.b64encode(f"mfa-player:{password}".encode()).decode()
    response = client.get(
        f"{prefix}/player/me/privacy-settings/",
        HTTP_AUTHORIZATION=f"Basic {credentials}",
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_admin_form_cannot_create_password_only_session(client: Client) -> None:
    """A correct staff password must go through the application's MFA flow."""
    password = secrets.token_urlsafe(24)
    user = User.objects.create_user(
        username="staff-mfa", password=password, is_staff=True
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    assert TwoFactorService.requires_two_factor(profile)
    response = client.post(
        "/admin/login/", {"username": user.username, "password": password}
    )
    assert response.status_code == HTTPStatus.FOUND
    assert response.url.endswith("/sign-in")
    assert "_auth_user_id" not in client.session


def test_admin_rejects_old_session_and_accepts_verified_session(client: Client) -> None:
    """Old password-only sessions cannot access admin after the rollout."""
    user = User.objects.create_user(username="admin-session", is_staff=True)
    client.force_login(user)
    assert client.get("/admin/").status_code == HTTPStatus.FOUND
    client.force_login(user)
    session = client.session
    session["bg_auth_mfa_verified"] = user.get_session_auth_hash()
    session.save()
    assert client.get("/admin/").status_code == HTTPStatus.OK


def test_refresh_replay_revokes_successor_and_access(client: Client) -> None:
    """Replaying a consumed refresh credential disables the entire device family."""
    user = User.objects.create_user(username="refresh-replay")
    pair = issue_pair(user)
    response = client.post(
        "/api/auth/jwt/refresh/",
        {"refresh": pair.refresh},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    successor = response.json()
    assert successor["refresh"] != pair.refresh
    assert (
        client.post(
            "/api/auth/jwt/refresh/",
            {"refresh": pair.refresh},
            content_type="application/json",
        ).status_code
        == HTTPStatus.UNAUTHORIZED
    )
    assert (
        client.post(
            "/api/auth/jwt/refresh/",
            {"refresh": successor["refresh"]},
            content_type="application/json",
        ).status_code
        == HTTPStatus.UNAUTHORIZED
    )
    assert (
        client.get(
            "/api/player/me/privacy-settings/",
            HTTP_AUTHORIZATION=f"Bearer {successor['access']}",
        ).status_code
        == HTTPStatus.UNAUTHORIZED
    )


def test_revocation_is_device_scoped_and_invalidates_access(client: Client) -> None:
    """Logout invalidates copied credentials without signing out another device."""
    user = User.objects.create_user(username="revoked-device")
    pair, other = issue_pair(user), issue_pair(user)
    assert (
        client.post(
            "/auth/jwt/revoke/",
            {"refresh": pair.refresh},
            content_type="application/json",
        ).status_code
        == HTTPStatus.OK
    )
    assert (
        client.get(
            "/api/player/me/privacy-settings/",
            HTTP_AUTHORIZATION=f"Bearer {pair.access}",
        ).status_code
        == HTTPStatus.UNAUTHORIZED
    )
    assert (
        client.get(
            "/api/player/me/privacy-settings/",
            HTTP_AUTHORIZATION=f"Bearer {other.access}",
        ).status_code
        == HTTPStatus.OK
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_refresh_has_one_winner_and_revokes_replayed_family() -> None:
    """Separate database connections cannot consume one refresh credential twice."""
    user = User.objects.create_user(username="concurrent-refresh")
    pair = issue_pair(user)
    payload = decode(pair.refresh, expected_type="refresh")
    barrier = Barrier(2)

    def attempt() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            rotate_refresh_token(payload, user)
        except JwtError:
            return False
        else:
            return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [False, True]
    assert not credentials_are_current(decode(pair.access), user)
