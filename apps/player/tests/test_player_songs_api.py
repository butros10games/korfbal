"""Tests for player song download endpoints."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
import pytest

from apps.player.models.player_song import PlayerSong, PlayerSongStatus


START_TIME_SECONDS = 42


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_songs_create_list_and_update(client: Client) -> None:
    """Authenticated players can create, list and update downloaded songs."""
    user = get_user_model().objects.create_user(
        username="song_user",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    create_response = client.post(
        "/api/player/me/songs/",
        data=json.dumps({"spotify_url": "https://open.spotify.com/track/1234567890"}),
        content_type="application/json",
    )
    assert create_response.status_code == HTTPStatus.CREATED
    created = create_response.json()
    assert created["spotify_url"].startswith("https://open.spotify.com/track/")
    assert created["start_time_seconds"] == 0
    assert created["status"] in {
        "queued",
        "downloading",
        "uploading",
        "ready",
        "failed",
    }

    list_response = client.get("/api/player/me/songs/")
    assert list_response.status_code == HTTPStatus.OK
    songs = list_response.json()
    assert isinstance(songs, list)
    assert len(songs) == 1
    assert songs[0]["id_uuid"] == created["id_uuid"]

    song_id = created["id_uuid"]
    patch_response = client.patch(
        f"/api/player/me/songs/{song_id}/",
        data=json.dumps({"start_time_seconds": START_TIME_SECONDS}),
        content_type="application/json",
    )
    assert patch_response.status_code == HTTPStatus.OK
    patched = patch_response.json()
    assert patched["start_time_seconds"] == START_TIME_SECONDS

    # Set song to failed status for retry test
    song = PlayerSong.objects.get(id_uuid=song_id)
    song.status = PlayerSongStatus.FAILED
    song.error_message = "Test failure"
    song.save()

    # Test retry
    retry_response = client.post(f"/api/player/me/songs/{song_id}/retry/")
    assert retry_response.status_code == HTTPStatus.OK
    retried = retry_response.json()
    assert retried["status"] == "queued"
    assert not retried["error_message"]

    # Test delete
    delete_response = client.delete(f"/api/player/me/songs/{song_id}/")
    assert delete_response.status_code == HTTPStatus.NO_CONTENT

    # Verify deleted
    list_after_delete = client.get("/api/player/me/songs/")
    assert list_after_delete.status_code == HTTPStatus.OK
    songs_after = list_after_delete.json()
    assert len(songs_after) == 0
