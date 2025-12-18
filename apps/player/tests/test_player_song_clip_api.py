"""Tests for the goal-song clip endpoint."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from subprocess import CompletedProcess  # nosec B404
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


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_clip_redirects_to_versioned_clip_when_generated(
    client: Client,
) -> None:
    """When ffmpeg works, the endpoint redirects to a deterministic v2 clip key."""
    user = get_user_model().objects.create_user(
        username="clip_user_2",
        password="pass1234",  # noqa: S106  # nosec
    )

    song = PlayerSong.objects.create(
        player=user.player,
        spotify_url="https://open.spotify.com/track/example",
        status=PlayerSongStatus.READY,
        start_time_seconds=12,
    )
    song.audio_file.save("test.mp3", ContentFile(b"not really audio"), save=True)

    captured: dict[str, object] = {}

    def fake_run(args: list[str], check: bool) -> CompletedProcess[str]:
        # ffmpeg output is always the last argument.
        out_path = Path(args[-1])
        out_path.write_bytes(b"fake mp3")
        captured["ffmpeg_args"] = args
        return CompletedProcess(args=args, returncode=0)

    # Make storage deterministic for the assertion.
    with (
        patch("apps.player.api.views.shutil.which", return_value="ffmpeg"),
        patch("apps.player.api.views.default_storage.exists", return_value=False),
        patch(
            "apps.player.api.views.default_storage.save",
            side_effect=lambda key, _f: key,
        ),
        patch(
            "apps.player.api.views.default_storage.url",
            side_effect=lambda key: f"/media/{key}",
        ),
        patch("apps.player.api.views.subprocess.run", side_effect=fake_run),
    ):
        response = client.get(
            f"/api/player/api/songs/{song.id_uuid}/clip/?start=12&duration=8"
        )

    assert response.status_code == HTTPStatus.FOUND
    assert "song_clips_v2" in response["Location"]
    assert str(song.id_uuid) in response["Location"]
    assert "start_12_dur_8.mp3" in response["Location"]

    ffmpeg_args = captured.get("ffmpeg_args")
    assert isinstance(ffmpeg_args, list)
    assert "-map_metadata" in ffmpeg_args
    assert "-map_chapters" in ffmpeg_args
