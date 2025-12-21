"""Tests for the current-player goal-song settings endpoint."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, override_settings
import pytest

from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus


SONG_A_START_TIME_SECONDS = 12


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_requires_authentication(client: Client) -> None:
    """The goal-song settings endpoint is authenticated."""
    response = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({"goal_song_uri": "https://example.invalid/x.mp3"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_rejects_non_object_json_payload(client: Client) -> None:
    """The endpoint should reject non-object JSON bodies (e.g. arrays)."""
    user = get_user_model().objects.create_user(
        username="goal_song_non_object",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps(["goal_song_uri"]),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Invalid payload"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_parsing_validation_errors(client: Client) -> None:
    """Type validation should produce clear 400s."""
    user = get_user_model().objects.create_user(
        username="goal_song_bad_types",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response_bad_uri = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({"goal_song_uri": 123}),
        content_type="application/json",
    )
    assert response_bad_uri.status_code == HTTPStatus.BAD_REQUEST
    assert response_bad_uri.json()["detail"] == "goal_song_uri must be a string or null"

    response_bad_start = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({"song_start_time": True}),
        content_type="application/json",
    )
    assert response_bad_start.status_code == HTTPStatus.BAD_REQUEST
    assert response_bad_start.json()["detail"] == (
        "song_start_time must be a number or null"
    )

    response_bad_ids = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({"goal_song_song_ids": "not-a-list"}),
        content_type="application/json",
    )
    assert response_bad_ids.status_code == HTTPStatus.BAD_REQUEST
    assert response_bad_ids.json()["detail"] == (
        "goal_song_song_ids must be a list of strings or null"
    )


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_sets_uri_and_start_time_from_selected_song(client: Client) -> None:
    """Selecting goal_song_song_ids should sync legacy fields to first selection."""
    user = get_user_model().objects.create_user(
        username="goal_song_select",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    song_a = PlayerSong.objects.create(
        player=user.player,
        spotify_url="",
        status=PlayerSongStatus.READY,
        start_time_seconds=SONG_A_START_TIME_SECONDS,
    )
    song_a.audio_file.save("a.mp3", ContentFile(b"a"), save=True)

    song_b = PlayerSong.objects.create(
        player=user.player,
        spotify_url="",
        status=PlayerSongStatus.READY,
        start_time_seconds=42,
    )
    song_b.audio_file.save("b.mp3", ContentFile(b"b"), save=True)

    response = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({
            "goal_song_song_ids": [
                str(song_a.id_uuid),
                "",
                str(song_a.id_uuid),
                str(song_b.id_uuid),
            ]
        }),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK

    user.player.refresh_from_db()
    assert user.player.goal_song_song_ids == [str(song_a.id_uuid), str(song_b.id_uuid)]
    assert user.player.song_start_time == SONG_A_START_TIME_SECONDS
    assert user.player.goal_song_uri == song_a.audio_file.url


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_rejects_unknown_or_not_ready_songs(client: Client) -> None:
    """Unknown ids or not-ready songs should be rejected with helpful payloads."""
    user = get_user_model().objects.create_user(
        username="goal_song_validate",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    ready_song = PlayerSong.objects.create(
        player=user.player,
        spotify_url="",
        status=PlayerSongStatus.READY,
        start_time_seconds=0,
    )
    ready_song.audio_file.save("ready.mp3", ContentFile(b"x"), save=True)

    unknown_id = "00000000-0000-0000-0000-000000000001"
    response_unknown = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({"goal_song_song_ids": [unknown_id]}),
        content_type="application/json",
    )

    assert response_unknown.status_code == HTTPStatus.BAD_REQUEST
    assert response_unknown.json()["detail"] == "Unknown song id(s)"
    assert response_unknown.json()["missing"] == [unknown_id]

    # Not ready: READY is required and the audio file must exist.
    not_ready = PlayerSong.objects.create(
        player=user.player,
        spotify_url="",
        status=PlayerSongStatus.FAILED,
        start_time_seconds=0,
    )

    response_not_ready = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({
            "goal_song_song_ids": [
                str(not_ready.id_uuid),
                str(ready_song.id_uuid),
            ]
        }),
        content_type="application/json",
    )

    assert response_not_ready.status_code == HTTPStatus.BAD_REQUEST
    assert response_not_ready.json()["detail"] == "Song(s) not ready"
    assert str(not_ready.id_uuid) in response_not_ready.json()["not_ready"]


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_can_clear_selection_and_fields(client: Client) -> None:
    """Sending null for goal_song_song_ids clears selection and legacy fields."""
    user = get_user_model().objects.create_user(
        username="goal_song_clear",
        password="pass1234",  # noqa: S106  # nosec
    )
    player: Player = user.player
    player.goal_song_song_ids = ["x"]
    player.goal_song_uri = "https://example.invalid/old.mp3"
    player.song_start_time = 10
    player.save(
        update_fields=["goal_song_song_ids", "goal_song_uri", "song_start_time"]
    )

    client.force_login(user)
    response = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({"goal_song_song_ids": None}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK

    player.refresh_from_db()
    assert player.goal_song_song_ids == []
    assert not player.goal_song_uri
    assert player.song_start_time is None
