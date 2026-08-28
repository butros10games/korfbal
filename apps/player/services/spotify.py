"""Application services for Spotify authorization and playback."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from http import HTTPStatus
import logging
import secrets
from typing import Any, cast
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.player.application.ports import SpotifyClient, SpotifyResponse
from apps.player.models.spotify_token import SpotifyToken
from apps.player.services.spotify_queries import spotify_token_for_user_id


logger = logging.getLogger(__name__)

SPOTIFY_NO_ACTIVE_DEVICE_DETAIL = (
    "No active Spotify device found. Open Spotify on your phone and try again."
)
SPOTIFY_SUCCESS_STATUSES = {
    HTTPStatus.OK,
    HTTPStatus.ACCEPTED,
    HTTPStatus.NO_CONTENT,
}


@dataclass(frozen=True, slots=True)
class SpotifyAuthorization:
    """Authorization URL and the state that must be stored in the session."""

    url: str
    state: str


@dataclass(frozen=True, slots=True)
class _SpotifyConnectionData:
    access_token: str
    refresh_token: str
    expires_at: datetime
    spotify_user_id: str


class SpotifyConnectionOutcome(Enum):
    """Stable outcomes for a provider authorization callback."""

    CONNECTED = "connected"
    INVALID_STATE = "invalid_state"
    PROVIDER_ERROR = "provider_error"
    ACCOUNT_CONFLICT = "account_conflict"


@dataclass(frozen=True, slots=True)
class SpotifyPlayCommand:
    """Untrusted playback values normalized by the application service."""

    track_uri: object
    position_ms: object = 0
    device_id: object = None


@dataclass(frozen=True, slots=True)
class SpotifyPauseCommand:
    """Untrusted pause values normalized by the application service."""

    device_id: object = None


@dataclass(frozen=True, slots=True)
class SpotifyInputError(Exception):
    """Raised when a playback command is missing required input."""

    detail: str


@dataclass(frozen=True, slots=True)
class SpotifyAccessError(Exception):
    """Raised when no usable Spotify access token is available."""

    detail: str


@dataclass(frozen=True, slots=True)
class SpotifyPlaybackError(Exception):
    """Provider-neutral playback failure returned to the HTTP adapter."""

    code: str
    detail: str
    conflict: bool = False


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


def create_spotify_authorization() -> SpotifyAuthorization:
    """Create one provider authorization request without touching HTTP session state."""
    state = secrets.token_urlsafe(24)
    return SpotifyAuthorization(
        url=build_spotify_authorize_url(state=state),
        state=state,
    )


def _response_payload(response: SpotifyResponse) -> Mapping[str, Any]:
    payload = response.json()
    return cast(Mapping[str, Any], payload) if isinstance(payload, Mapping) else {}


def _expires_in_seconds(payload: Mapping[str, Any]) -> int:
    raw = payload.get("expires_in", 3600)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3600


def _expires_at(payload: Mapping[str, Any]) -> datetime:
    return timezone.now() + timedelta(seconds=max(0, _expires_in_seconds(payload) - 60))


def _refresh_spotify_access_token(
    token: SpotifyToken,
    *,
    client: SpotifyClient,
) -> SpotifyToken:
    response = client.post_token(
        data={
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "client_secret": settings.SPOTIFY_CLIENT_SECRET,
        }
    )
    response.raise_for_status()
    payload = _response_payload(response)
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise SpotifyAccessError("Spotify token refresh failed")

    token.access_token = access_token
    refreshed_refresh = payload.get("refresh_token")
    if isinstance(refreshed_refresh, str) and refreshed_refresh:
        token.refresh_token = refreshed_refresh
    token.expires_at = _expires_at(payload)
    token.save(update_fields=["access_token", "refresh_token", "expires_at"])
    return token


@transaction.atomic
def ensure_spotify_access_token(
    user: AbstractBaseUser,
    *,
    client: SpotifyClient,
) -> str:
    """Return a fresh access token while serializing concurrent refreshes.

    Raises:
        SpotifyAccessError: When Spotify is not connected or refresh fails.

    """
    user_id = getattr(user, "pk", None)
    if not isinstance(user_id, int):
        raise SpotifyAccessError("Spotify not connected")

    token = spotify_token_for_user_id(user_id, for_update=True)
    if token is None:
        raise SpotifyAccessError("Spotify not connected")
    if token.is_token_expired():
        try:
            token = _refresh_spotify_access_token(token, client=client)
        except SpotifyAccessError:
            raise
        except Exception as exc:
            logger.warning(
                "Spotify token refresh failed for user %s",
                user_id,
                exc_info=True,
            )
            raise SpotifyAccessError("Spotify token refresh failed") from exc
    return token.access_token


@transaction.atomic
def _persist_spotify_connection(
    *,
    user_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    spotify_user_id: str,
) -> None:
    SpotifyToken.objects.update_or_create(
        user_id=user_id,
        defaults={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "spotify_user_id": spotify_user_id,
        },
    )


def _authorization_credentials(
    *,
    code: object,
    state: object,
    expected_state: object,
) -> tuple[str, str] | None:
    if not all(
        isinstance(value, str) and value for value in (code, state, expected_state)
    ):
        return None
    if not secrets.compare_digest(cast(str, state), cast(str, expected_state)):
        return None
    return cast(str, code), cast(str, state)


def _exchange_spotify_connection(
    *,
    code: str,
    client: SpotifyClient,
) -> _SpotifyConnectionData:
    token_response = client.post_token(
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "client_secret": settings.SPOTIFY_CLIENT_SECRET,
        }
    )
    token_response.raise_for_status()
    token_payload = _response_payload(token_response)
    access_token = str(token_payload.get("access_token") or "")
    refresh_token = str(token_payload.get("refresh_token") or "")
    if not access_token or not refresh_token:
        raise ValueError("Spotify token response is missing credentials")

    profile_response = client.get_current_user_profile(access_token=access_token)
    profile_response.raise_for_status()
    spotify_user_id = str(_response_payload(profile_response).get("id") or "")
    if not spotify_user_id:
        raise ValueError("Spotify profile response is missing an account ID")

    return _SpotifyConnectionData(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_expires_at(token_payload),
        spotify_user_id=spotify_user_id,
    )


def complete_spotify_authorization(
    *,
    user: AbstractBaseUser,
    code: object,
    state: object,
    expected_state: object,
    client: SpotifyClient,
) -> SpotifyConnectionOutcome:
    """Validate, exchange, and persist one Spotify authorization callback."""
    credentials = _authorization_credentials(
        code=code,
        state=state,
        expected_state=expected_state,
    )
    if credentials is None:
        return SpotifyConnectionOutcome.INVALID_STATE

    user_id = getattr(user, "pk", None)
    if not isinstance(user_id, int):
        return SpotifyConnectionOutcome.PROVIDER_ERROR

    try:
        connection = _exchange_spotify_connection(
            code=credentials[0],
            client=client,
        )
    except Exception:
        logger.warning(
            "Spotify authorization exchange failed for user %s",
            user_id,
            exc_info=True,
        )
        return SpotifyConnectionOutcome.PROVIDER_ERROR

    try:
        _persist_spotify_connection(
            user_id=user_id,
            access_token=connection.access_token,
            refresh_token=connection.refresh_token,
            expires_at=connection.expires_at,
            spotify_user_id=connection.spotify_user_id,
        )
    except IntegrityError:
        return SpotifyConnectionOutcome.ACCOUNT_CONFLICT

    return SpotifyConnectionOutcome.CONNECTED


def _device_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _position_ms(value: object) -> int:
    try:
        if isinstance(value, (str, int, float)):
            return max(0, int(float(value)))
    except (TypeError, ValueError):
        pass
    return 0


def _spotify_error_message(response: SpotifyResponse) -> str:
    try:
        error = _response_payload(response).get("error")
    except (TypeError, ValueError):
        return ""
    if not isinstance(error, Mapping):
        return ""
    return str(error.get("message") or "")


def play_spotify(
    *,
    user: AbstractBaseUser,
    command: SpotifyPlayCommand,
    client: SpotifyClient,
) -> None:
    """Start Spotify playback or raise a provider-neutral application error.

    Raises:
        SpotifyInputError: When the track URI is absent.
        SpotifyPlaybackError: When the provider rejects playback.

    """
    if not isinstance(command.track_uri, str) or not command.track_uri.strip():
        raise SpotifyInputError("track_uri is required")

    access_token = ensure_spotify_access_token(user, client=client)
    try:
        response = client.put_playback(
            access_token=access_token,
            action="play",
            device_id=_device_id(command.device_id),
            json_body={
                "uris": [normalise_spotify_track_uri(command.track_uri)],
                "position_ms": _position_ms(command.position_ms),
            },
        )
    except Exception as exc:
        raise SpotifyPlaybackError(
            code="spotify_play_failed",
            detail="Spotify play failed",
        ) from exc

    if response.status_code in SPOTIFY_SUCCESS_STATUSES:
        return

    spotify_message = _spotify_error_message(response)
    if (
        response.status_code == HTTPStatus.NOT_FOUND
        and "no active device" in spotify_message.lower()
    ):
        raise SpotifyPlaybackError(
            code="no_active_device",
            detail=SPOTIFY_NO_ACTIVE_DEVICE_DETAIL,
            conflict=True,
        )
    raise SpotifyPlaybackError(
        code="spotify_play_failed",
        detail=spotify_message or response.text or "Spotify play failed",
    )


def pause_spotify(
    *,
    user: AbstractBaseUser,
    command: SpotifyPauseCommand,
    client: SpotifyClient,
) -> None:
    """Pause Spotify playback or raise a provider-neutral application error.

    Raises:
        SpotifyPlaybackError: When the provider rejects playback.

    """
    access_token = ensure_spotify_access_token(user, client=client)
    try:
        response = client.put_playback(
            access_token=access_token,
            action="pause",
            device_id=_device_id(command.device_id),
        )
    except Exception as exc:
        raise SpotifyPlaybackError(
            code="spotify_pause_failed",
            detail="Spotify pause failed",
        ) from exc

    if response.status_code not in SPOTIFY_SUCCESS_STATUSES:
        raise SpotifyPlaybackError(
            code="spotify_pause_failed",
            detail=response.text or "Spotify pause failed",
        )
