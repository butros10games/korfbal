"""Tests for the goal-song clip endpoint."""

from __future__ import annotations

from collections.abc import Sequence
from http import HTTPStatus
from pathlib import Path
from subprocess import CompletedProcess  # nosec B404
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, override_settings
import pytest

from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.command_runner import CommandRunOptions


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_clip_reports_unavailable_when_ffmpeg_missing(
    client: Client,
) -> None:
    """The tracker must not mistake an unclipped full song for a prepared clip."""
    user = get_user_model().objects.create_user(
        username="clip_user",
        password="pass1234",  # nosec
    )

    song = PlayerSong.objects.create(
        player=user.player,
        spotify_url="https://open.spotify.com/track/example",
        status=PlayerSongStatus.READY,
        start_time_seconds=12,
    )

    # Store any file so `audio_file.url` exists.
    song.audio_file.save("test.mp3", ContentFile(b"not really audio"), save=True)

    # Force the ffmpeg path to be missing deterministically.
    with patch("apps.player.services.player_audio.find_ffmpeg", return_value=None):
        response = client.get(
            f"/api/player/api/songs/{song.id_uuid}/clip/?start=12&duration=8&stream=1"
        )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "Goal sound clip is not prepared."}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_clip_keeps_media_redirect_for_regular_audio_elements(
    client: Client,
) -> None:
    """Non-tracker previews should retain storage Range-request behavior."""
    user = get_user_model().objects.create_user(
        username="clip_redirect_user",
        password="pass1234",  # nosec
    )
    song = PlayerSong.objects.create(
        player=user.player,
        status=PlayerSongStatus.READY,
    )
    song.audio_file.save("redirect.mp3", ContentFile(b"audio"), save=True)

    with patch("apps.player.services.player_audio.find_ffmpeg", return_value=None):
        response = client.get(
            f"/api/player/api/songs/{song.id_uuid}/clip/?start=0&duration=8"
        )

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == song.audio_file.url


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_clip_streams_versioned_clip_when_generated(
    client: Client,
) -> None:
    """When ffmpeg works, the endpoint streams and caches the prepared clip."""
    user = get_user_model().objects.create_user(
        username="clip_user_2",
        password="pass1234",  # nosec
    )

    song = PlayerSong.objects.create(
        player=user.player,
        spotify_url="https://open.spotify.com/track/example",
        status=PlayerSongStatus.READY,
        start_time_seconds=12,
    )
    song.audio_file.save("test.mp3", ContentFile(b"not really audio"), save=True)

    captured: dict[str, object] = {}

    def fake_run(
        args: Sequence[str],
        options: CommandRunOptions,
    ) -> CompletedProcess[str]:
        assert options.check is True
        args_list = list(args)
        # ffmpeg output is always the last argument.
        out_path = Path(args_list[-1])
        out_path.write_bytes(b"fake mp3")
        captured["ffmpeg_args"] = args_list
        return CompletedProcess(args=args_list, returncode=0)

    # Make storage deterministic for the assertion.
    with (
        patch("apps.player.services.player_audio.find_ffmpeg", return_value="ffmpeg"),
        patch(
            "apps.player.services.player_audio.default_storage.exists",
            return_value=False,
        ),
        patch(
            "apps.player.services.audio_clipper.DEFAULT_COMMAND_RUNNER.run",
            side_effect=fake_run,
        ),
    ):
        response = client.get(
            f"/api/player/api/songs/{song.id_uuid}/clip/?start=12&duration=8&stream=1"
        )

    assert response.status_code == HTTPStatus.OK
    assert b"".join(response.streaming_content) == b"fake mp3"
    assert response["X-Goal-Audio-Prepared"] == "1"
    assert response["Cache-Control"] == "private, max-age=31536000, immutable"

    ffmpeg_args = captured.get("ffmpeg_args")
    assert isinstance(ffmpeg_args, list)
    assert "-map_metadata" in ffmpeg_args
    assert "-map_chapters" in ffmpeg_args


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_clip_reuses_existing_cached_clip(client: Client) -> None:
    """Existing clip files should be reused without regenerating them."""
    user = get_user_model().objects.create_user(
        username="clip_user_cached",
        password="pass1234",  # nosec
    )

    song = PlayerSong.objects.create(
        player=user.player,
        spotify_url="https://open.spotify.com/track/example",
        status=PlayerSongStatus.READY,
        start_time_seconds=12,
    )
    song.audio_file.save("test.mp3", ContentFile(b"not really audio"), save=True)

    with (
        patch("apps.player.services.player_audio.find_ffmpeg", return_value="ffmpeg"),
        patch(
            "apps.player.services.player_audio.default_storage.exists",
            return_value=True,
        ),
        patch(
            "apps.player.api.views.songs.default_storage.open",
            return_value=ContentFile(b"existing clip"),
        ),
        patch(
            "apps.player.services.player_audio.transcode_to_mp3_clip_file"
        ) as mocked_transcode,
    ):
        response = client.get(
            f"/api/player/api/songs/{song.id_uuid}/clip/?start=12&duration=8&stream=1"
        )

    assert response.status_code == HTTPStatus.OK
    assert b"".join(response.streaming_content) == b"existing clip"
    assert response["X-Goal-Audio-Prepared"] == "1"
    mocked_transcode.assert_not_called()
