"""Requests-backed Spotify adapter."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import requests

from apps.player.services.spotify import SpotifyClient, SpotifyResponse


class RequestsSpotifyClient:
    """Production Spotify client backed by requests."""

    def post_token(self, *, data: dict[str, Any]) -> SpotifyResponse:
        """POST to the Spotify token endpoint."""
        return requests.post(
            "https://accounts.spotify.com/api/token",
            data=data,
            timeout=10,
        )

    def get_current_user_profile(self, *, access_token: str) -> SpotifyResponse:
        """GET the current Spotify user profile."""
        return requests.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )

    def put_playback(
        self,
        *,
        access_token: str,
        action: str,
        device_id: str | None,
        json_body: dict[str, Any] | None = None,
    ) -> SpotifyResponse:
        """PUT to a Spotify playback endpoint."""
        query = f"?{urlencode({'device_id': device_id})}" if device_id else ""
        return requests.put(
            f"https://api.spotify.com/v1/me/player/{action}{query}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=json_body,
            timeout=10,
        )


DEFAULT_SPOTIFY_CLIENT: SpotifyClient = RequestsSpotifyClient()
