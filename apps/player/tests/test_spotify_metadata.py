"""Provider contract tests with synthetic credentials and HTTP responses."""

from http import HTTPStatus
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import override_settings
import pytest

from apps.player.adapters.outbound.spotify_tracks import RequestsTrackMetadataClient


TRACK_ID = "27CXrzqx1N44o1Pi6AHRT4"


def test_credentials_stay_on_fixed_token_endpoint_and_track_is_resolved() -> None:
    """Redirects cannot forward the secret or access token to another origin."""
    secret = uuid4().hex
    with (
        override_settings(SPOTIFY_CLIENT_ID="synthetic", SPOTIFY_CLIENT_SECRET=secret),
        patch(
            "apps.player.adapters.outbound.spotify_tracks.requests.post",
            return_value=Mock(
                status_code=HTTPStatus.OK,
                json=lambda: {"access_token": "synthetic-token"},
            ),
        ) as post,
        patch(
            "apps.player.adapters.outbound.spotify_tracks.requests.get",
            return_value=Mock(
                status_code=HTTPStatus.OK,
                json=lambda: {"name": "Track", "artists": [{"name": "Artist"}]},
            ),
        ) as get,
    ):
        result = RequestsTrackMetadataClient().get_track(TRACK_ID)
    assert result.title == "Track"
    assert result.artists == ("Artist",)
    post.assert_called_once_with(
        "https://accounts.spotify.com/api/token",
        auth=("synthetic", secret),
        data={"grant_type": "client_credentials"},
        timeout=10,
        allow_redirects=False,
    )
    get.assert_called_once_with(
        f"https://api.spotify.com/v1/tracks/{TRACK_ID}",
        headers={"Authorization": "Bearer synthetic-token"},
        timeout=10,
        allow_redirects=False,
    )


@pytest.mark.parametrize("track_id", ["..", "x?next=internal", "a/b", "a" * 23])
def test_invalid_identifier_never_contacts_provider(track_id: str) -> None:
    """Untrusted IDs cannot become paths or query parameters on provider calls."""
    with (
        patch("apps.player.adapters.outbound.spotify_tracks.requests.post") as post,
        pytest.raises(ValueError, match="Invalid Spotify track ID"),
    ):
        RequestsTrackMetadataClient().get_track(track_id)
    post.assert_not_called()


def test_missing_credentials_fail_without_using_bundled_public_secrets() -> None:
    """Imports fail clearly when the official metadata client is not configured."""
    with (
        override_settings(SPOTIFY_CLIENT_ID="", SPOTIFY_CLIENT_SECRET=""),
        pytest.raises(RuntimeError, match="server credentials"),
    ):
        RequestsTrackMetadataClient().get_track(TRACK_ID)


@pytest.mark.parametrize("payload", [{}, {"name": "Track", "artists": []}, []])
def test_malformed_metadata_is_a_controlled_error(payload: object) -> None:
    """Bad upstream content cannot reach the downloader."""
    with (
        override_settings(
            SPOTIFY_CLIENT_ID="synthetic", SPOTIFY_CLIENT_SECRET=uuid4().hex
        ),
        patch(
            "apps.player.adapters.outbound.spotify_tracks.requests.post",
            return_value=Mock(
                status_code=HTTPStatus.OK,
                json=lambda: {"access_token": "synthetic-token"},
            ),
        ),
        patch(
            "apps.player.adapters.outbound.spotify_tracks.requests.get",
            return_value=Mock(status_code=HTTPStatus.OK, json=lambda: payload),
        ),
        pytest.raises(RuntimeError, match="metadata"),
    ):
        RequestsTrackMetadataClient().get_track(TRACK_ID)
