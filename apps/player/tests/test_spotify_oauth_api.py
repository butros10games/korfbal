"""Tests for Spotify OAuth endpoints.

These tests intentionally avoid calling Spotify; they validate server-side
configuration gates and session state handling.
"""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth.models import User
from django.test import Client, override_settings
import pytest


NOT_CONFIGURED_VALUE = ""


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_spotify_connect_requires_authentication(client: Client) -> None:
    """Spotify connect endpoint should require authentication."""
    response = client.get("/api/player/spotify/connect/")
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=NOT_CONFIGURED_VALUE,
    SPOTIFY_CLIENT_SECRET=NOT_CONFIGURED_VALUE,
)
def test_spotify_connect_returns_400_when_not_configured(client: Client) -> None:
    """When Spotify is not configured, the endpoint should return a 400."""
    user = User.objects.create_user(
        username="spotify_not_configured",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    response = client.get("/api/player/spotify/connect/")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Spotify is not configured on the server"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID="client_id",
    SPOTIFY_CLIENT_SECRET="client_secret",  # nosec
    SPOTIFY_REDIRECT_URI="https://example.invalid/oauth/callback",
    WEB_APP_ORIGIN="https://app.example.invalid",
)
def test_spotify_connect_sets_oauth_state_and_optional_redirect(client: Client) -> None:
    """The connect endpoint should generate state and store redirect in session."""
    user = User.objects.create_user(
        username="spotify_connect",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    response = client.get(
        "/api/player/spotify/connect/?redirect=/settings",
        secure=True,
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["url"].startswith("https://accounts.spotify.com/authorize?")

    session = client.session
    assert "spotify_oauth_state" in session
    assert session.get("spotify_oauth_redirect") == "/settings"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID="client_id",
    SPOTIFY_CLIENT_SECRET="client_secret",  # nosec
    SPOTIFY_REDIRECT_URI="https://example.invalid/oauth/callback",
)
@pytest.mark.parametrize(
    "redirect",
    ["https://evil.example/", "//evil.example/"],
)
def test_spotify_connect_ignores_non_relative_redirect(
    client: Client,
    redirect: str,
) -> None:
    """Only relative redirect paths should be stored for safety."""
    user = User.objects.create_user(
        username="spotify_connect_redirect",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    response = client.get(
        f"/api/player/spotify/connect/?redirect={redirect}",
        secure=True,
    )

    assert response.status_code == HTTPStatus.OK
    session = client.session
    assert "spotify_oauth_redirect" not in session
    assert "spotify_oauth_state" in session
