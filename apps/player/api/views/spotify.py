"""HTTP adapters for Spotify authorization and playback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser
from django.http import HttpResponseRedirect
from rest_framework import permissions, status
from rest_framework.exceptions import ParseError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.composition import (
    complete_spotify_authorization,
    create_spotify_authorization,
    pause_spotify,
    play_spotify,
)
from apps.player.services.spotify import (
    SpotifyAccessError,
    SpotifyConnectionOutcome,
    SpotifyInputError,
    SpotifyPauseCommand,
    SpotifyPlaybackError,
    SpotifyPlayCommand,
    spotify_enabled,
)

from .common import (
    AUTHENTICATION_REQUIRED_DETAIL,
    SPOTIFY_NOT_CONFIGURED_DETAIL,
    redirect_to_frontend,
)


def _request_payload(request: Request) -> Mapping[str, Any]:
    """Return an object-shaped request body.

    Raises:
        ParseError: If the request body is not a JSON object.

    """
    payload = request.data
    if not isinstance(payload, Mapping):
        raise ParseError("Request body must be a JSON object")
    return payload


def _relative_redirect_path(value: object) -> str | None:
    """Return a local absolute path while rejecting scheme-relative URLs."""
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        return value
    return None


def _authenticated_user(request: Request) -> AbstractBaseUser | None:
    user = request.user
    return user if isinstance(user, AbstractBaseUser) else None


def _spotify_error_response(
    exc: SpotifyInputError | SpotifyAccessError | SpotifyPlaybackError,
) -> Response:
    if isinstance(exc, SpotifyPlaybackError):
        return Response(
            {"code": exc.code, "detail": exc.detail},
            status=(
                status.HTTP_409_CONFLICT
                if exc.conflict
                else status.HTTP_400_BAD_REQUEST
            ),
        )
    return Response({"detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)


class SpotifyConnectAPIView(KorfbalAPIView):
    """Start the Spotify OAuth flow."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return an authorization URL and persist its single-use state."""
        if not spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        authorization = create_spotify_authorization()
        request.session["spotify_oauth_state"] = authorization.state
        redirect_path = _relative_redirect_path(request.query_params.get("redirect"))
        if redirect_path is not None:
            request.session["spotify_oauth_redirect"] = redirect_path
        request.session.modified = True
        return Response({"url": authorization.url})


class SpotifyCallbackView(KorfbalAPIView):
    """Complete a Spotify OAuth callback."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponseRedirect:
        """Consume callback state, persist the connection, and return to the SPA."""
        if not spotify_enabled():
            return redirect_to_frontend()

        expected_state = request.session.pop("spotify_oauth_state", None)
        if expected_state is not None:
            request.session.modified = True
        user = _authenticated_user(request)
        if user is None:
            return redirect_to_frontend()

        outcome = complete_spotify_authorization(
            user=user,
            code=request.query_params.get("code"),
            state=request.query_params.get("state"),
            expected_state=expected_state,
        )
        if outcome is not SpotifyConnectionOutcome.CONNECTED:
            return redirect_to_frontend()

        redirect_path = _relative_redirect_path(request.query_params.get("redirect"))
        if redirect_path is None:
            redirect_path = _relative_redirect_path(
                request.session.pop("spotify_oauth_redirect", None)
            )
            request.session.modified = True
        return redirect_to_frontend(redirect_path)


class SpotifyPlayAPIView(KorfbalAPIView):
    """Trigger Spotify playback for the connected user."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Start playback on the user's active or requested device."""
        if not spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = _request_payload(request)
        user = _authenticated_user(request)
        if user is None:
            return Response(
                AUTHENTICATION_REQUIRED_DETAIL,
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            play_spotify(
                user=user,
                command=SpotifyPlayCommand(
                    track_uri=payload.get("track_uri"),
                    position_ms=payload.get("position_ms", 0),
                    device_id=payload.get("device_id"),
                ),
            )
        except (SpotifyInputError, SpotifyAccessError, SpotifyPlaybackError) as exc:
            return _spotify_error_response(exc)
        return Response({"ok": True})


class SpotifyPauseAPIView(KorfbalAPIView):
    """Pause Spotify playback for the connected user."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Pause playback on the user's active or requested device."""
        if not spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = _request_payload(request)
        user = _authenticated_user(request)
        if user is None:
            return Response(
                AUTHENTICATION_REQUIRED_DETAIL,
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            pause_spotify(
                user=user,
                command=SpotifyPauseCommand(device_id=payload.get("device_id")),
            )
        except (SpotifyAccessError, SpotifyPlaybackError) as exc:
            return _spotify_error_response(exc)
        return Response({"ok": True})
