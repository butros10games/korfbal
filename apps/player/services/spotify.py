"""Spotify integration helpers for player API endpoints."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import secrets
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.utils import timezone
import requests

from apps.player.models.spotify_token import SpotifyToken


SPOTIFY_NO_ACTIVE_DEVICE_DETAIL = (
    "No active Spotify device found. Open Spotify on your phone and try again."
)


def spotify_enabled() -> bool:
    """Return whether Spotify credentials are configured."""
    return bool(settings.SPOTIFY_CLIENT_ID and settings.SPOTIFY_CLIENT_SECRET)


def normalise_spotify_track_uri(value: str) -> str:
    """Normalize open.spotify.com track URLs to spotify:track URIs."""
    raw = value.strip()
    if raw.startswith("spotify:track:"):
        return raw

    if "open.spotify.com/track/" in raw:
        track_id = raw.split("open.spotify.com/track/")[-1].split("?")[0].split("/")[0]
        if track_id:
            return f"spotify:track:{track_id}"

    return raw


def get_or_create_spotify_oauth_state(request: Any) -> str:
    """Generate and persist an OAuth state token in the session."""
    state = secrets.token_urlsafe(24)
    request.session["spotify_oauth_state"] = state
    request.session.modified = True
    return state


def build_spotify_authorize_url(*, state: str) -> str:
    """Build the Spotify OAuth authorization URL."""
    scopes = (
        "user-read-email user-read-private user-read-playback-state "
        "user-modify-playback-state user-read-currently-playing"
    )

    params = {
        "response_type": "code",
        "client_id": settings.SPOTIFY_CLIENT_ID,
        "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
        "scope": scopes,
        "state": state,
    }
    return f"https://accounts.spotify.com/authorize?{urlencode(params)}"


def _get_spotify_token(user: AbstractBaseUser) -> SpotifyToken | None:
    return SpotifyToken.objects.filter(user=user).first()


def _refresh_spotify_access_token(token: SpotifyToken) -> SpotifyToken:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
        "client_id": settings.SPOTIFY_CLIENT_ID,
        "client_secret": settings.SPOTIFY_CLIENT_SECRET,
    }
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data=payload,
        timeout=10,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()

    access_token = str(data.get("access_token") or "")
    expires_in = int(data.get("expires_in") or 3600)
    if not access_token:
        raise RuntimeError("Spotify token refresh failed")

    token.access_token = access_token
    refreshed_refresh = data.get("refresh_token")
    if isinstance(refreshed_refresh, str) and refreshed_refresh:
        token.refresh_token = refreshed_refresh

    token.expires_at = timezone.now() + timedelta(seconds=max(0, expires_in - 60))
    token.save(update_fields=["access_token", "refresh_token", "expires_at"])
    return token


def ensure_spotify_access_token(user: AbstractBaseUser) -> str:
    """Return a fresh Spotify access token for the user.

    Raises:
        RuntimeError: When Spotify is not connected or refresh fails.

    """
    token = _get_spotify_token(user)
    if token is None:
        raise RuntimeError("Spotify not connected")
    if token.is_token_expired():
        token = _refresh_spotify_access_token(token)
    return token.access_token


def exchange_callback_code_for_user(
    *,
    user: AbstractBaseUser,
    code: str,
) -> bool:
    """Exchange a Spotify callback code and persist the resulting token."""
    token_response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "client_secret": settings.SPOTIFY_CLIENT_SECRET,
        },
        timeout=10,
    )
    token_response.raise_for_status()
    token_data: dict[str, Any] = token_response.json()

    access_token = str(token_data.get("access_token") or "")
    refresh_token = str(token_data.get("refresh_token") or "")
    expires_in = int(token_data.get("expires_in") or 3600)
    if not access_token or not refresh_token:
        return False

    profile_response = requests.get(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    profile_response.raise_for_status()
    profile: dict[str, Any] = profile_response.json()
    spotify_user_id = str(profile.get("id") or "")
    if not spotify_user_id:
        return False

    expires_at = timezone.now() + timedelta(seconds=max(0, expires_in - 60))

    SpotifyToken.objects.update_or_create(
        user=user,
        defaults={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "spotify_user_id": spotify_user_id,
        },
    )
    return True


def start_spotify_playback(
    *,
    access_token: str,
    track_uri: str,
    position_ms: int,
    device_id: str | None,
) -> requests.Response:
    """Start Spotify playback for a track on the active or given device."""
    query = f"?{urlencode({'device_id': device_id})}" if device_id else ""
    return requests.put(
        f"https://api.spotify.com/v1/me/player/play{query}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "uris": [track_uri],
            "position_ms": position_ms,
        },
        timeout=10,
    )


def pause_spotify_playback(
    *,
    access_token: str,
    device_id: str | None,
) -> requests.Response:
    """Pause Spotify playback on the active or given device."""
    query = f"?{urlencode({'device_id': device_id})}" if device_id else ""
    return requests.put(
        f"https://api.spotify.com/v1/me/player/pause{query}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )


def spotify_play_error_payload(
    play_response: requests.Response,
) -> tuple[int, dict[str, str]]:
    """Map Spotify playback failures to the public API payload."""
    detail = play_response.text or "Spotify play failed"

    spotify_message = ""
    try:
        spotify_payload: dict[str, Any] = play_response.json()
        spotify_error = spotify_payload.get("error")
        if isinstance(spotify_error, dict):
            spotify_message = str(spotify_error.get("message") or "")
    except (ValueError, TypeError):
        spotify_message = ""

    if (
        play_response.status_code == HTTPStatus.NOT_FOUND
        and "no active device" in spotify_message.lower()
    ):
        return 409, {
            "code": "no_active_device",
            "detail": SPOTIFY_NO_ACTIVE_DEVICE_DETAIL,
        }

    return 400, {
        "code": "spotify_play_failed",
        "detail": spotify_message or detail,
    }
