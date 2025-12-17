"""Tests for the goal-song clip endpoint."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, override_settings
import pytest

from apps.player.models.player_song import PlayerSong, PlayerSongStatus


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_clip_falls_back_when_ffmpeg_missing(client: Client) -> None:
    """When ffmpeg isn't available, the clip endpoint should redirect to full audio."""
    user = get_user_model().objects.create_user(
        username="clip_user",
        password="pass1234",  # noqa: S106  # nosec
    )

    song = PlayerSong.objects.create(
        player=user.player,
        spotify_url="https://open.spotify.com/track/example",
        status=PlayerSongStatus.READY,
        start_time_seconds=12,
    )

    # Store any file so `audio_file.url` exists.
    song.audio_file.save("test.mp3", ContentFile(b"not really audio"), save=True)

    # Force the ffmpeg path to fail deterministically.
    with patch("apps.player.api.views.subprocess.run", side_effect=FileNotFoundError):
        response = client.get(
            f"/api/player/api/songs/{song.id_uuid}/clip/?start=12&duration=8"
        )

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == song.audio_file.url
