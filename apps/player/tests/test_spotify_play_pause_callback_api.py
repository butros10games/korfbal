"""Regression tests for Spotify play/pause/callback endpoints.

These tests avoid external HTTP calls by patching `requests`.
"""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import json
import secrets
from typing import Any

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
import pytest

from apps.player.models.spotify_token import SpotifyToken


SPOTIFY_CLIENT_ID = "client_id"
SPOTIFY_CLIENT_SECRET = "client_secret"  # noqa: S105  # nosec
SPOTIFY_REDIRECT_URI = "https://example.invalid/oauth/callback"
WEB_APP_ORIGIN = "https://app.example.invalid"

NOT_CONFIGURED_VALUE = ""


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


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_spotify_play_requires_auth(client: Client) -> None:
    """Play endpoint is authenticated."""
    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:123"}),
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=NOT_CONFIGURED_VALUE,
    SPOTIFY_CLIENT_SECRET=NOT_CONFIGURED_VALUE,
)
def test_spotify_play_returns_400_when_not_configured(client: Client) -> None:
    """Not-configured servers should return a clean 400."""
    user = get_user_model().objects.create_user(
        username="spotify_play_not_configured",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:123"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Spotify is not configured on the server"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
)
def test_spotify_play_requires_track_uri(client: Client) -> None:
    """track_uri is required and must be a non-empty string."""
    user = get_user_model().objects.create_user(
        username="spotify_play_missing_track",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "track_uri is required"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
)
def test_spotify_play_returns_400_when_not_connected(client: Client) -> None:
    """When no token exists, the endpoint should return a 400 (not 500)."""
    user = get_user_model().objects.create_user(
        username="spotify_play_not_connected",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:123"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Spotify not connected"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
)
def test_spotify_play_normalises_open_spotify_track_url(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open.spotify.com URLs should be normalized to spotify:track URIs."""
    user = get_user_model().objects.create_user(
        username="spotify_play_normalise",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    SpotifyToken.objects.create(
        user=user,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(hours=1),
        spotify_user_id="spotify_user",
    )

    captured: dict[str, object] = {}

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResponse(status_code=204)

    monkeypatch.setattr("apps.player.api.views.requests.put", _fake_put)

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


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
)
def test_spotify_play_no_active_device_returns_409(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spotify's 'no active device' case should map to a 409 with code."""
    user = get_user_model().objects.create_user(
        username="spotify_play_no_device",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    SpotifyToken.objects.create(
        user=user,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(hours=1),
        spotify_user_id="spotify_user",
    )

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(
            status_code=404,
            text="",
            json_data={"error": {"message": "No active device found"}},
        )

    monkeypatch.setattr("apps.player.api.views.requests.put", _fake_put)

    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:abc"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    payload = response.json()
    assert payload["code"] == "no_active_device"
    assert "No active Spotify device" in payload["detail"]


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
)
def test_spotify_play_other_error_returns_400(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other Spotify errors should return a 400 with a stable code."""
    user = get_user_model().objects.create_user(
        username="spotify_play_other_error",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    SpotifyToken.objects.create(
        user=user,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(hours=1),
        spotify_user_id="spotify_user",
    )

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(
            status_code=400,
            text="bad",
            json_data={"error": {"message": "Bad request"}},
        )

    monkeypatch.setattr("apps.player.api.views.requests.put", _fake_put)

    response = client.post(
        "/api/player/spotify/play/",
        data=json.dumps({"track_uri": "spotify:track:abc"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "spotify_play_failed"
    assert payload["detail"] == "Bad request"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_spotify_pause_requires_auth(client: Client) -> None:
    """Pause endpoint is authenticated."""
    response = client.post(
        "/api/player/spotify/pause/",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
)
def test_spotify_pause_failure_is_best_effort_400(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause errors should not crash; they return a permissive 400 payload."""
    user = get_user_model().objects.create_user(
        username="spotify_pause_error",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    SpotifyToken.objects.create(
        user=user,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(hours=1),
        spotify_user_id="spotify_user",
    )

    def _fake_put(url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(status_code=500, text="server error")

    monkeypatch.setattr("apps.player.api.views.requests.put", _fake_put)

    response = client.post(
        "/api/player/spotify/pause/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "spotify_pause_failed"
    assert payload["detail"] == "server error"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI=SPOTIFY_REDIRECT_URI,
    WEB_APP_ORIGIN=WEB_APP_ORIGIN,
)
def test_spotify_callback_state_mismatch_redirects_home(client: Client) -> None:
    """State mismatch should redirect to frontend root without creating tokens."""
    user = get_user_model().objects.create_user(
        username="spotify_cb_state_mismatch",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    session = client.session
    session["spotify_oauth_state"] = "expected"
    session.save()

    response = client.get(
        "/api/player/spotify/callback/?code=abc&state=wrong",
        secure=True,
    )

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == f"{WEB_APP_ORIGIN}/"
    assert not SpotifyToken.objects.filter(user=user).exists()


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    SPOTIFY_CLIENT_ID=SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET=SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI=SPOTIFY_REDIRECT_URI,
    WEB_APP_ORIGIN=WEB_APP_ORIGIN,
)
def test_spotify_callback_happy_path_creates_token_and_redirects(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid callback should store tokens and redirect to stored path."""
    user = get_user_model().objects.create_user(
        username="spotify_cb_ok",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    session = client.session
    session["spotify_oauth_state"] = "expected"
    session["spotify_oauth_redirect"] = "/settings"
    session.save()

    def _fake_post(url: str, **kwargs: object) -> _FakeResponse:
        assert "accounts.spotify.com/api/token" in url
        access_token = secrets.token_urlsafe(16)
        refresh_token = secrets.token_urlsafe(16)
        _fake_post.access_token = access_token  # type: ignore[attr-defined]
        _fake_post.refresh_token = refresh_token  # type: ignore[attr-defined]
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

    monkeypatch.setattr("apps.player.api.views.requests.post", _fake_post)
    monkeypatch.setattr("apps.player.api.views.requests.get", _fake_get)

    response = client.get(
        "/api/player/spotify/callback/?code=abc&state=expected",
        secure=True,
    )

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == f"{WEB_APP_ORIGIN}/settings"

    token = SpotifyToken.objects.filter(user=user).first()
    assert token is not None
    assert token.spotify_user_id == "spotify_user"
    assert token.access_token == _fake_post.access_token  # type: ignore[attr-defined]
    assert token.refresh_token == _fake_post.refresh_token  # type: ignore[attr-defined]
