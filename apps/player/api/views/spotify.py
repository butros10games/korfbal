"""Spotify OAuth and playback API views."""

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
from apps.player.composition import spotify_client
from apps.player.services.spotify import (
    build_spotify_authorize_url,
    ensure_spotify_access_token,
    exchange_callback_code_for_user,
    get_or_create_spotify_oauth_state,
    normalise_spotify_track_uri,
    pause_spotify_playback,
    spotify_enabled,
    spotify_play_error_payload,
    start_spotify_playback,
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


class SpotifyConnectAPIView(KorfbalAPIView):
    """Start Spotify OAuth flow by returning an authorization URL."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return a Spotify OAuth authorization URL for the authenticated user."""
        if not spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        redirect_path = request.query_params.get("redirect")
        if isinstance(redirect_path, str) and redirect_path.startswith("/"):
            request.session["spotify_oauth_redirect"] = redirect_path
            request.session.modified = True

        return Response({
            "url": build_spotify_authorize_url(
                state=get_or_create_spotify_oauth_state(request)
            )
        })


class SpotifyCallbackView(KorfbalAPIView):
    """Handle Spotify OAuth callback requests."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponseRedirect:
        """Handle the Spotify OAuth callback and persist tokens."""
        if not spotify_enabled():
            return redirect_to_frontend()

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        expected_state = request.session.get("spotify_oauth_state")
        if not code or not state or not expected_state or state != expected_state:
            return redirect_to_frontend()

        user = request.user
        if not isinstance(user, AbstractBaseUser):
            return redirect_to_frontend()

        if not exchange_callback_code_for_user(
            user=user,
            code=str(code),
            client=spotify_client,
        ):
            return redirect_to_frontend()

        redirect_path = request.query_params.get("redirect")
        if not (isinstance(redirect_path, str) and redirect_path.startswith("/")):
            redirect_path = request.session.pop("spotify_oauth_redirect", None)
            request.session.modified = True

        return redirect_to_frontend(
            redirect_path if isinstance(redirect_path, str) else None,
        )


class SpotifyPlayAPIView(KorfbalAPIView):
    """Trigger Spotify playback for the connected user."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Start playback on the user's active Spotify Connect device."""
        if not spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = _request_payload(request)
        track_uri_raw = payload.get("track_uri")
        if not isinstance(track_uri_raw, str) or not track_uri_raw.strip():
            return Response(
                {"detail": "track_uri is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        position_ms_raw = payload.get("position_ms", 0)
        try:
            position_ms = int(float(position_ms_raw))
        except (TypeError, ValueError):
            position_ms = 0
        position_ms = max(0, position_ms)

        try:
            user = request.user
            if not isinstance(user, AbstractBaseUser):
                return Response(
                    AUTHENTICATION_REQUIRED_DETAIL,
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            access_token = ensure_spotify_access_token(
                user,
                client=spotify_client,
            )
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        device_id = payload.get("device_id")
        play_response = start_spotify_playback(
            access_token=access_token,
            track_uri=normalise_spotify_track_uri(track_uri_raw),
            position_ms=position_ms,
            device_id=device_id if isinstance(device_id, str) and device_id else None,
            client=spotify_client,
        )

        if play_response.status_code not in {200, 202, 204}:
            status_code, payload = spotify_play_error_payload(play_response)
            return Response(payload, status=status_code)

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
        """Pause playback on the user's active Spotify Connect device."""
        if not spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = _request_payload(request)
        try:
            user = request.user
            if not isinstance(user, AbstractBaseUser):
                return Response(
                    AUTHENTICATION_REQUIRED_DETAIL,
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            access_token = ensure_spotify_access_token(
                user,
                client=spotify_client,
            )
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        device_id = payload.get("device_id")
        pause_response = pause_spotify_playback(
            access_token=access_token,
            device_id=device_id if isinstance(device_id, str) and device_id else None,
            client=spotify_client,
        )

        if pause_response.status_code not in {200, 202, 204}:
            detail = pause_response.text or "Spotify pause failed"
            return Response(
                {"code": "spotify_pause_failed", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"ok": True})
