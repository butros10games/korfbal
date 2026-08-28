"""Application-service tests for the Spotify provider boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
import secrets
from typing import Any

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
import pytest

from apps.player.models.spotify_token import SpotifyToken
from apps.player.services.spotify import (
    SpotifyAccessError,
    SpotifyConnectionOutcome,
    SpotifyPlaybackError,
    SpotifyPlayCommand,
    complete_spotify_authorization,
    play_spotify,
)


@dataclass(slots=True)
class _FakeResponse:
    status_code: int = HTTPStatus.OK
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= HTTPStatus.BAD_REQUEST:
            raise RuntimeError("provider request failed")


@dataclass(slots=True)
class _FakeSpotifyClient:
    token_response: _FakeResponse = field(default_factory=_FakeResponse)
    profile_response: _FakeResponse = field(default_factory=_FakeResponse)
    playback_response: _FakeResponse = field(
        default_factory=lambda: _FakeResponse(status_code=HTTPStatus.NO_CONTENT)
    )
    token_error: Exception | None = None
    playback_error: Exception | None = None
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    playback_requests: list[dict[str, Any]] = field(default_factory=list)

    def post_token(self, *, data: dict[str, Any]) -> _FakeResponse:
        self.token_requests.append(data)
        if self.token_error is not None:
            raise self.token_error
        return self.token_response

    def get_current_user_profile(self, *, access_token: str) -> _FakeResponse:
        return self.profile_response

    def put_playback(
        self,
        *,
        access_token: str,
        action: str,
        device_id: str | None,
        json_body: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        self.playback_requests.append({
            "access_token": access_token,
            "action": action,
            "device_id": device_id,
            "json_body": json_body,
        })
        if self.playback_error is not None:
            raise self.playback_error
        return self.playback_response


@pytest.mark.django_db
@override_settings(SPOTIFY_CLIENT_ID="client", SPOTIFY_CLIENT_SECRET="secret")
def test_play_refreshes_and_persists_an_expired_connection() -> None:
    """Playback refreshes once and uses the newly persisted access token."""
    user = User.objects.create_user(username="spotify_refresh")
    old_access_token = secrets.token_urlsafe(16)
    refresh_token = secrets.token_urlsafe(16)
    new_access_token = secrets.token_urlsafe(16)
    token = SpotifyToken.objects.create(
        user=user,
        access_token=old_access_token,
        refresh_token=refresh_token,
        expires_at=timezone.now() - timedelta(minutes=1),
        spotify_user_id="refresh-account",
    )
    client = _FakeSpotifyClient(
        token_response=_FakeResponse(
            payload={"access_token": new_access_token, "expires_in": 3600}
        )
    )

    play_spotify(
        user=user,
        command=SpotifyPlayCommand(track_uri="spotify:track:track-id"),
        client=client,
    )

    token.refresh_from_db()
    assert token.access_token == new_access_token
    assert token.refresh_token == refresh_token
    assert client.token_requests == [
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "client",
            "client_secret": "secret",
        }
    ]
    assert client.playback_requests[0]["access_token"] == new_access_token


@pytest.mark.django_db
def test_refresh_transport_failure_becomes_stable_access_error() -> None:
    """Provider transport details do not leak through the application boundary."""
    user = User.objects.create_user(username="spotify_refresh_error")
    SpotifyToken.objects.create(
        user=user,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() - timedelta(minutes=1),
        spotify_user_id="refresh-error-account",
    )
    client = _FakeSpotifyClient(token_error=TimeoutError("provider unavailable"))

    with pytest.raises(SpotifyAccessError, match="Spotify token refresh failed"):
        play_spotify(
            user=user,
            command=SpotifyPlayCommand(track_uri="spotify:track:track-id"),
            client=client,
        )

    assert client.playback_requests == []


@pytest.mark.django_db
def test_authorization_rejects_malformed_provider_credentials() -> None:
    """Incomplete provider payloads never create a local connection."""
    user = User.objects.create_user(username="spotify_malformed")
    client = _FakeSpotifyClient(
        token_response=_FakeResponse(payload={"access_token": "access-only"})
    )

    outcome = complete_spotify_authorization(
        user=user,
        code="code",
        state="state",
        expected_state="state",
        client=client,
    )

    assert outcome is SpotifyConnectionOutcome.PROVIDER_ERROR
    assert not SpotifyToken.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_authorization_does_not_reassign_an_existing_spotify_account() -> None:
    """One provider account cannot silently move between local users."""
    owner = User.objects.create_user(username="spotify_owner")
    challenger = User.objects.create_user(username="spotify_challenger")
    SpotifyToken.objects.create(
        user=owner,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(hours=1),
        spotify_user_id="shared-account",
    )
    client = _FakeSpotifyClient(
        token_response=_FakeResponse(
            payload={
                "access_token": secrets.token_urlsafe(16),
                "refresh_token": secrets.token_urlsafe(16),
                "expires_in": 3600,
            }
        ),
        profile_response=_FakeResponse(payload={"id": "shared-account"}),
    )

    outcome = complete_spotify_authorization(
        user=challenger,
        code="code",
        state="state",
        expected_state="state",
        client=client,
    )

    assert outcome is SpotifyConnectionOutcome.ACCOUNT_CONFLICT
    assert SpotifyToken.objects.filter(user=owner).exists()
    assert not SpotifyToken.objects.filter(user=challenger).exists()


@pytest.mark.django_db
def test_playback_transport_failure_becomes_stable_playback_error() -> None:
    """Playback transport failures expose a stable error code and message."""
    user = User.objects.create_user(username="spotify_play_error")
    SpotifyToken.objects.create(
        user=user,
        access_token=secrets.token_urlsafe(16),
        refresh_token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(hours=1),
        spotify_user_id="play-error-account",
    )
    client = _FakeSpotifyClient(playback_error=TimeoutError("provider unavailable"))

    with pytest.raises(SpotifyPlaybackError) as captured:
        play_spotify(
            user=user,
            command=SpotifyPlayCommand(track_uri="spotify:track:track-id"),
            client=client,
        )

    assert captured.value.code == "spotify_play_failed"
    assert captured.value.detail == "Spotify play failed"
