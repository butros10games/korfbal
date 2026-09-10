"""Fetch track metadata from fixed Spotify endpoints using server credentials."""

from http import HTTPStatus
import re

from django.conf import settings
import requests

from apps.player.application.ports import TrackMetadata


class RequestsTrackMetadataClient:
    """Keep credentials in HTTPS requests, never in downloader argv or logs."""

    def get_track(self, track_id: str) -> TrackMetadata:
        """Load a bounded track identity through the official catalogue API.

        Raises:
            ValueError: The track ID is malformed.
            RuntimeError: Credentials or usable provider metadata are unavailable.

        """
        if not re.fullmatch(r"[A-Za-z0-9]{22}", track_id):
            raise ValueError("Invalid Spotify track ID.")
        client_id = settings.SPOTIFY_CLIENT_ID
        client_secret = settings.SPOTIFY_CLIENT_SECRET
        if not client_id or not client_secret:
            raise RuntimeError("Spotify track imports require server credentials.")
        try:
            token_response = requests.post(
                "https://accounts.spotify.com/api/token",
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                timeout=10,
                allow_redirects=False,
            )
            if token_response.status_code != HTTPStatus.OK:
                raise RuntimeError("Spotify authentication is unavailable.")
            token = token_response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Spotify authentication is unavailable.")
            response = requests.get(
                f"https://api.spotify.com/v1/tracks/{track_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code != HTTPStatus.OK:
                raise RuntimeError("Spotify track metadata is unavailable.")
            data = response.json()
            title = data.get("name")
            artists = data.get("artists")
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(artists, list)
            ):
                raise RuntimeError("Spotify returned incomplete track metadata.")
            names = tuple(
                artist["name"].strip()
                for artist in artists
                if isinstance(artist, dict)
                and isinstance(artist.get("name"), str)
                and artist["name"].strip()
            )
            if not names:
                raise RuntimeError("Spotify returned incomplete track metadata.")
            return TrackMetadata(
                title=title.strip()[:300],
                artists=tuple(name[:300] for name in names[:10]),
            )
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            raise RuntimeError(
                "Spotify track metadata is unavailable. Please retry."
            ) from None
