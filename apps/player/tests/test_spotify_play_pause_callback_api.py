"""Regression tests for Spotify play/pause/callback endpoints.

These tests avoid external HTTP calls by patching `requests`.
"""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import json
import secrets
from typing import Any

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils import timezone
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.player.models.spotify_token import SpotifyToken


SPOTIFY_CLIENT_ID = "client_id"
SPOTIFY_CLIENT_SECRET = "client_secret"  # nosec
SPOTIFY_REDIRECT_URI = "https://example.invalid/oauth/callback"
WEB_APP_ORIGIN = "https://app.example.invalid"

NOT_CONFIGURED_VALUE = ""


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configured_spotify(settings: SettingsWrapper) -> None:
    """Configure synthetic Spotify credentials for this module."""
    settings.SPOTIFY_CLIENT_ID = SPOTIFY_CLIENT_ID
    settings.SPOTIFY_CLIENT_SECRET = SPOTIFY_CLIENT_SECRET
    settings.SPOTIFY_REDIRECT_URI = SPOTIFY_REDIRECT_URI
    settings.WEB_APP_ORIGIN = WEB_APP_ORIGIN


@pytest.fixture
def user(client: Client) -> User:
    """Authenticate a passwordless user without a Spotify connection."""
    user = User.objects.create_user(username="spotify_user")
    client.force_login(user)
    return user


@pytest.fixture
def connected_spotify(user: User) -> None:
    """Give an authenticated user an unexpired synthetic Spotify token."""
    SpotifyToken.objects.create(
        user=user,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(hours=1),
        spotify_user_id="spotify_user",
    )


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        text: str = "",
        json_data: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data or {}

    def json(self) -> dict[str, Any]:
        return self._json_data

    def raise_for_status(self) -> None:
        if int(self.status_code) >= HTTPStatus.BAD_REQUEST:
            raise RuntimeError("http")


def test_spotify_play_requires_auth(client: Client) -> None:
    """Play endpoint is authenticated."""
    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:123"}),
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["Content-Type"].startswith("application/json")


@override_settings(
    SPOTIFY_CLIENT_ID=NOT_CONFIGURED_VALUE,
    SPOTIFY_CLIENT_SECRET=NOT_CONFIGURED_VALUE,
)
def test_spotify_play_returns_400_when_not_configured(
    client: Client, user: User
) -> None:
    """Not-configured servers should return a clean 400."""
    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:123"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Spotify is not configured on the server"


def test_spotify_play_requires_track_uri(client: Client, user: User) -> None:
    """track_uri is required and must be a non-empty string."""
    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "track_uri is required"


@pytest.mark.parametrize(
    "path",
    [
        "/api/player/spotify/play/",
        "/api/player/spotify/pause/",
    ],
)
def test_spotify_playback_rejects_json_array(
    client: Client, user: User, path: str
) -> None:
    """Playback endpoints require an object-shaped JSON body."""
    response = client.post(
        path,
        data=json.dumps(["unexpected"]),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Request body must be a JSON object"


def test_spotify_play_returns_400_when_not_connected(
    client: Client, user: User
) -> None:
    """When no token exists, the endpoint should return a 400 (not 500)."""
    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:123"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Spotify not connected"


@pytest.mark.usefixtures("connected_spotify")
def test_spotify_play_normalises_open_spotify_track_url(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open.spotify.com URLs should be normalized to spotify:track URIs."""
    captured: dict[str, object] = {}

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResponse(status_code=204)

    monkeypatch.setattr(
        "apps.player.adapters.outbound.spotify.requests.put",
        _fake_put,
    )

    position_ms = 123
    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({
            "track_uri": "https://open.spotify.com/track/abc123?si=x",
            "position_ms": f"{position_ms}.0",
        }),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"ok": True}

    payload = captured.get("json")
    assert isinstance(payload, dict)
    assert payload["uris"] == ["spotify:track:abc123"]
    assert payload["position_ms"] == position_ms


@pytest.mark.usefixtures("connected_spotify")
def test_spotify_play_no_active_device_returns_409(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spotify's 'no active device' case should map to a 409 with code."""

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(
            status_code=404,
            text="",
            json_data={"error": {"message": "No active device found"}},
        )

    monkeypatch.setattr(
        "apps.player.adapters.outbound.spotify.requests.put",
        _fake_put,
    )

    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:abc"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    payload = response.json()
    assert payload["code"] == "no_active_device"
    assert "No active Spotify device" in payload["detail"]


@pytest.mark.usefixtures("connected_spotify")
def test_spotify_play_other_error_returns_400(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other Spotify errors should return a 400 with a stable code."""

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(
            status_code=400,
            text="bad",
            json_data={"error": {"message": "Bad request"}},
        )

    monkeypatch.setattr(
        "apps.player.adapters.outbound.spotify.requests.put",
        _fake_put,
    )

    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:abc"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "spotify_play_failed"
    assert payload["detail"] == "Bad request"


def test_spotify_pause_requires_auth(client: Client) -> None:
    """Pause endpoint is authenticated."""
    response = client.post(
        "/api/player/spotify/pause/",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["Content-Type"].startswith("application/json")


@pytest.mark.usefixtures("connected_spotify")
def test_spotify_pause_failure_is_best_effort_400(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause errors should not crash; they return a permissive 400 payload."""

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(status_code=500, text="server error")

    monkeypatch.setattr(
        "apps.player.adapters.outbound.spotify.requests.put",
        _fake_put,
    )

    response = client.post(
        "/api/player/spotify/pause/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "spotify_pause_failed"
    assert payload["detail"] == "server error"


def test_spotify_callback_state_mismatch_redirects_home(
    client: Client, user: User
) -> None:
    """State mismatch should redirect to frontend root without creating tokens."""
    session = client.session
    session["spotify_oauth_state"] = "expected"
    session.save()

    response = client.get(
        "/api/player/spotify/callback/?code=abc&state=wrong",
        secure=True,
    )

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == f"{WEB_APP_ORIGIN}/"
    assert "spotify_oauth_state" not in client.session
    assert not SpotifyToken.objects.filter(user=user).exists()


def test_spotify_callback_happy_path_creates_token_and_redirects(
    client: Client,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid callback should store tokens and redirect to stored path."""
    session = client.session
    session["spotify_oauth_state"] = "expected"
    session["spotify_oauth_redirect"] = "/settings"
    session.save()
    token_exchange_calls = 0
    access_token = secrets.token_urlsafe(16)
    refresh_token = secrets.token_urlsafe(16)

    def _fake_post(url: str, **kwargs: object) -> _FakeResponse:
        nonlocal token_exchange_calls
        token_exchange_calls += 1
        assert "accounts.spotify.com/api/token" in url
        return _FakeResponse(
            status_code=200,
            json_data={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": 3600,
            },
        )

    def _fake_get(url: str, **kwargs: object) -> _FakeResponse:
        assert "api.spotify.com/v1/me" in url
        return _FakeResponse(status_code=200, json_data={"id": "spotify_user"})

    monkeypatch.setattr(
        "apps.player.adapters.outbound.spotify.requests.post",
        _fake_post,
    )
    monkeypatch.setattr(
        "apps.player.adapters.outbound.spotify.requests.get",
        _fake_get,
    )

    response = client.get(
        "/api/player/spotify/callback/?code=abc&state=expected",
        secure=True,
    )

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == f"{WEB_APP_ORIGIN}/settings"
    assert "spotify_oauth_state" not in client.session

    token = SpotifyToken.objects.filter(user=user).first()
    assert token is not None
    assert token.spotify_user_id == "spotify_user"
    assert token.access_token == access_token
    assert token.refresh_token == refresh_token

    replay = client.get(
        "/api/player/spotify/callback/?code=abc&state=expected",
        secure=True,
    )

    assert replay.status_code == HTTPStatus.FOUND
    assert replay["Location"] == f"{WEB_APP_ORIGIN}/"
    assert token_exchange_calls == 1
