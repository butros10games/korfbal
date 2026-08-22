"""Regression tests for player upload endpoints."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
import pytest

from apps.player.models import PlayerSong, PlayerSongStatus
from apps.player.models.player import Player


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_profile_picture_requires_auth(client: Client) -> None:
    """Upload profile picture endpoint is authenticated."""
    response = client.post("/api/player/api/upload_profile_picture/")
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_profile_picture_missing_file_returns_400(client: Client) -> None:
    """Missing file should return a clean 400 with an error message."""
    user = get_user_model().objects.create_user(
        username="upload_profile_picture_missing",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    response = client.post("/api/player/api/upload_profile_picture/")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"error": "No profile_picture uploaded"}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_profile_picture_player_missing_returns_404(
    client: Client,
) -> None:
    """If the user has no Player row, the endpoint returns 404 (not 500)."""
    user = get_user_model().objects.create_user(
        username="upload_profile_picture_no_player",
        password="pass1234",  # nosec
    )
    Player.objects.filter(user=user).delete()
    client.force_login(user)

    uploaded = SimpleUploadedFile(
        "avatar.png",
        b"\x89PNG\r\n\x1a\n",
        content_type="image/png",
    )

    response = client.post(
        "/api/player/api/upload_profile_picture/",
        data={"profile_picture": uploaded},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {"error": "Player not found"}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_profile_picture_happy_path_persists_file_and_returns_url(
    client: Client,
    tmp_path: Path,
) -> None:
    """Successful upload stores the file and returns its URL."""
    user = get_user_model().objects.create_user(
        username="upload_profile_picture_ok",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    uploaded = SimpleUploadedFile(
        "avatar.png",
        b"\x89PNG\r\n\x1a\n",
        content_type="image/png",
    )

    with override_settings(MEDIA_ROOT=tmp_path, MEDIA_URL="/media/"):
        response = client.post(
            "/api/player/api/upload_profile_picture/",
            data={"profile_picture": uploaded},
        )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["url"].startswith("/media/profile_pictures/")
    assert "avatar" in payload["url"]
    assert payload["url"].endswith(".png")

    user.refresh_from_db()
    assert user.player.profile_picture
    assert user.player.profile_picture.name.startswith("profile_pictures/avatar")
    assert user.player.profile_picture.name.endswith(".png")


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_goal_song_requires_auth(client: Client) -> None:
    """Upload goal song endpoint is authenticated."""
    response = client.post("/api/player/api/upload_goal_song/")
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_goal_song_missing_file_returns_400(client: Client) -> None:
    """Missing goal_song file should return a clean 400."""
    user = get_user_model().objects.create_user(
        username="upload_goal_song_missing",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    response = client.post("/api/player/api/upload_goal_song/")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"error": "No goal_song uploaded"}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_goal_song_rejects_unsupported_content_type(client: Client) -> None:
    """If a content type is provided and is not allowed, reject it."""
    user = get_user_model().objects.create_user(
        username="upload_goal_song_unsupported_type",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    uploaded = SimpleUploadedFile(
        "goal_song.txt",
        b"hello",
        content_type="text/plain",
    )

    response = client.post(
        "/api/player/api/upload_goal_song/",
        data={"goal_song": uploaded},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {
        "error": "Unsupported audio type",
        "content_type": "text/plain",
    }


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_goal_song_player_missing_returns_404(client: Client) -> None:
    """If the user has no Player row, goal-song upload returns 404."""
    user = get_user_model().objects.create_user(
        username="upload_goal_song_no_player",
        password="pass1234",  # nosec
    )
    Player.objects.filter(user=user).delete()
    client.force_login(user)

    uploaded = SimpleUploadedFile(
        "goal_song.mp3",
        b"ID3",
        content_type="audio/mpeg",
    )

    response = client.post(
        "/api/player/api/upload_goal_song/",
        data={"goal_song": uploaded},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {"error": "Player not found"}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_upload_goal_song_happy_path_sanitizes_name_and_updates_player(
    client: Client,
    tmp_path: Path,
) -> None:
    """The legacy endpoint should create and select a modern PlayerSong."""
    user = get_user_model().objects.create_user(
        username="upload_goal_song_ok",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    uploaded = SimpleUploadedFile(
        "  cool song (1).MP3 ",
        b"ID3",
        content_type="AuDiO/MpEg",
    )

    with (
        override_settings(MEDIA_ROOT=tmp_path, MEDIA_URL="/media/"),
        patch("apps.player.services.player_songs.prepare_player_song_clip"),
    ):
        response = client.post(
            "/api/player/api/upload_goal_song/",
            data={"goal_song": uploaded},
        )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    user.refresh_from_db()
    song = PlayerSong.objects.get(player=user.player)
    assert song.status == PlayerSongStatus.READY
    assert song.audio_file.name.startswith("player_songs/")
    assert Path(song.audio_file.name).stem.startswith("coolsong1")
    assert Path(song.audio_file.name).suffix == ".MP3"
    assert user.player.goal_song_song_ids == [str(song.id_uuid)]
    assert user.player.goal_song_uri == payload["url"]
    assert payload["url"].endswith(song.audio_file.name)
    assert payload["player"]["goal_song_uri"] == payload["url"]
