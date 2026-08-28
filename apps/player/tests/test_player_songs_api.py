"""Tests for player song download endpoints."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus
import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
import pytest

from apps.player.api.serializers import PlayerSongSerializer
from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.player_song_queries import player_songs_for_player


START_TIME_SECONDS = 42
SECOND_SONG_START_TIME_SECONDS = 12
PLAYBACK_SPEED = 1.25
QueryCounter = Callable[[int], AbstractContextManager[None]]


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_songs_create_list_and_update(client: Client) -> None:
    """Authenticated players can create, list and update downloaded songs."""
    user = get_user_model().objects.create_user(
        username="song_user",
        password="pass1234",  # nosec
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
    assert created["playback_speed"] == pytest.approx(1.0)
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
        data=json.dumps({
            "start_time_seconds": START_TIME_SECONDS,
            "playback_speed": PLAYBACK_SPEED,
        }),
        content_type="application/json",
    )
    assert patch_response.status_code == HTTPStatus.OK
    patched = patch_response.json()
    assert patched["start_time_seconds"] == START_TIME_SECONDS
    assert patched["playback_speed"] == pytest.approx(PLAYBACK_SPEED)

    # Set song to failed status for retry test.
    # Effective status is CachedSong when linked.
    song = PlayerSong.objects.select_related("cached_song").get(id_uuid=song_id)
    if song.cached_song is not None:
        song.cached_song.status = CachedSongStatus.FAILED
        song.cached_song.error_message = "Test failure"
        song.cached_song.save()
    else:
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


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_delete_does_not_affect_other_players(client: Client) -> None:
    """Deleting a song removes only the current player's song."""
    user_a = get_user_model().objects.create_user(
        username="song_user_a",
        password="pass1234",  # nosec
    )
    user_b = get_user_model().objects.create_user(
        username="song_user_b",
        password="pass1234",  # nosec
    )

    client.force_login(user_a)
    create_a = client.post(
        "/api/player/me/songs/",
        data=json.dumps({"spotify_url": "https://open.spotify.com/track/111"}),
        content_type="application/json",
    )
    assert create_a.status_code == HTTPStatus.CREATED
    song_a_id = create_a.json()["id_uuid"]

    client.force_login(user_b)
    create_b = client.post(
        "/api/player/me/songs/",
        data=json.dumps({"spotify_url": "https://open.spotify.com/track/111"}),
        content_type="application/json",
    )
    assert create_b.status_code == HTTPStatus.CREATED
    song_b_id = create_b.json()["id_uuid"]
    assert song_b_id != song_a_id

    # Delete B's song.
    delete_b = client.delete(f"/api/player/me/songs/{song_b_id}/")
    assert delete_b.status_code == HTTPStatus.NO_CONTENT
    assert not PlayerSong.objects.filter(id_uuid=song_b_id).exists()

    # A's song must still exist.
    assert PlayerSong.objects.filter(id_uuid=song_a_id).exists()

    # Listing for each user should reflect their own data.
    client.force_login(user_a)
    list_a = client.get("/api/player/me/songs/")
    assert list_a.status_code == HTTPStatus.OK
    assert [row["id_uuid"] for row in list_a.json()] == [song_a_id]

    client.force_login(user_b)
    list_b = client.get("/api/player/me/songs/")
    assert list_b.status_code == HTTPStatus.OK
    assert list_b.json() == []


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_missing_player_song_patch_returns_not_found_before_validation(
    client: Client,
) -> None:
    """Song ownership lookup remains the first PATCH application check."""
    user = get_user_model().objects.create_user(
        username="missing_song_patch",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    response = client.patch(
        "/api/player/me/songs/00000000-0000-0000-0000-000000000001/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {"detail": "Song not found"}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_patch_hides_another_players_song(client: Client) -> None:
    """Song settings commands must enforce ownership at the HTTP boundary."""
    original_start_seconds = 4
    owner = get_user_model().objects.create_user(username="song-patch-owner")
    other = get_user_model().objects.create_user(username="song-patch-other")
    song = PlayerSong.objects.create(
        player=owner.player,
        start_time_seconds=original_start_seconds,
    )
    client.force_login(other)

    response = client.patch(
        f"/api/player/me/songs/{song.id_uuid}/",
        data=json.dumps({"start_time_seconds": 20}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    song.refresh_from_db()
    assert song.start_time_seconds == original_start_seconds


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_delete_cleans_goal_song_selection(client: Client) -> None:
    """Deleting a selected goal-song removes it from Player.goal_song_song_ids."""
    user = get_user_model().objects.create_user(
        username="song_user_goal_clean",
        password="pass1234",  # nosec
    )
    client.force_login(user)

    create_one = client.post(
        "/api/player/me/songs/",
        data=json.dumps({"spotify_url": "https://open.spotify.com/track/aaa"}),
        content_type="application/json",
    )
    assert create_one.status_code == HTTPStatus.CREATED
    song_one_id = create_one.json()["id_uuid"]

    create_two = client.post(
        "/api/player/me/songs/",
        data=json.dumps({"spotify_url": "https://open.spotify.com/track/bbb"}),
        content_type="application/json",
    )
    assert create_two.status_code == HTTPStatus.CREATED
    song_two_id = create_two.json()["id_uuid"]

    player: Player = user.player
    player.goal_song_song_ids = [song_one_id, song_two_id]
    player.goal_song_uri = "https://example.invalid/old.mp3"
    player.song_start_time = 10
    player.save(
        update_fields=[
            "goal_song_song_ids",
            "goal_song_uri",
            "song_start_time",
        ]
    )

    # Ensure second song has a non-default start time so we can verify resync.
    PlayerSong.objects.filter(id_uuid=song_two_id).update(
        start_time_seconds=SECOND_SONG_START_TIME_SECONDS
    )

    delete_response = client.delete(f"/api/player/me/songs/{song_one_id}/")
    assert delete_response.status_code == HTTPStatus.NO_CONTENT

    player.refresh_from_db()
    assert player.goal_song_song_ids == [song_two_id]
    assert player.song_start_time == SECOND_SONG_START_TIME_SECONDS
    song_two = PlayerSong.objects.select_related("cached_song").get(id_uuid=song_two_id)
    audio_file = song_two.effective_audio_file
    expected_uri = audio_file.url if audio_file else ""
    assert player.goal_song_uri == expected_uri


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_uses_cached_song_across_users(client: Client) -> None:
    """The same Spotify track should be downloaded once and reused across users."""
    user_a = get_user_model().objects.create_user(
        username="cache_user_a",
        password="pass1234",  # nosec
    )
    user_b = get_user_model().objects.create_user(
        username="cache_user_b",
        password="pass1234",  # nosec
    )

    spotify_url = "https://open.spotify.com/track/the_same_track"

    client.force_login(user_a)
    create_a = client.post(
        "/api/player/me/songs/",
        data=json.dumps({"spotify_url": spotify_url}),
        content_type="application/json",
    )
    assert create_a.status_code in {HTTPStatus.CREATED, HTTPStatus.OK}
    song_a_id = create_a.json()["id_uuid"]

    client.force_login(user_b)
    create_b = client.post(
        "/api/player/me/songs/",
        data=json.dumps({"spotify_url": spotify_url}),
        content_type="application/json",
    )
    assert create_b.status_code in {HTTPStatus.CREATED, HTTPStatus.OK}
    song_b_id = create_b.json()["id_uuid"]
    assert song_b_id != song_a_id

    song_a = PlayerSong.objects.select_related("cached_song").get(id_uuid=song_a_id)
    song_b = PlayerSong.objects.select_related("cached_song").get(id_uuid=song_b_id)
    assert song_a.cached_song_id is not None
    assert song_a.cached_song_id == song_b.cached_song_id

    cached_count = CachedSong.objects.filter(
        spotify_url__contains="open.spotify.com/track/"
    ).count()
    assert cached_count >= 1

    # Deleting one user's entry should not delete the shared cached record.
    cached_id = song_a.cached_song_id
    delete_b = client.delete(f"/api/player/me/songs/{song_b_id}/")
    assert delete_b.status_code == HTTPStatus.NO_CONTENT
    assert CachedSong.objects.filter(id_uuid=cached_id).exists()


@pytest.mark.django_db
def test_player_song_serialization_uses_the_query_read_model(
    django_assert_num_queries: QueryCounter,
) -> None:
    """Effective cached metadata must serialize without hidden queries."""
    user = get_user_model().objects.create_user(username="song-query-serializer")
    cached = CachedSong.objects.create(
        spotify_url="https://open.spotify.com/track/query-serializer",
        title="Cached title",
        artists="Cached artist",
        duration_seconds=120,
        status=CachedSongStatus.READY,
        audio_file="cached_songs/query-serializer.mp3",
    )
    song = PlayerSong.objects.create(player=user.player, cached_song=cached)
    loaded = player_songs_for_player(user.player).get(id_uuid=song.id_uuid)

    with django_assert_num_queries(0):
        payload = PlayerSongSerializer(loaded).data

    assert payload["title"] == "Cached title"
    assert payload["artists"] == "Cached artist"
    assert payload["status"] == CachedSongStatus.READY


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_song_list_queries_do_not_scale_with_song_count(client: Client) -> None:
    """The song list endpoint must not add queries per serialized song."""
    user = get_user_model().objects.create_user(username="song-query-list")
    client.force_login(user)
    PlayerSong.objects.create(player=user.player, title="First")

    with CaptureQueriesContext(connection) as initial_queries:
        initial_response = client.get("/api/player/me/songs/")
    for index in range(4):
        PlayerSong.objects.create(player=user.player, title=f"Extra {index}")
    with CaptureQueriesContext(connection) as expanded_queries:
        expanded_response = client.get("/api/player/me/songs/")

    assert initial_response.status_code == HTTPStatus.OK
    assert expanded_response.status_code == HTTPStatus.OK
    assert len(expanded_queries) == len(initial_queries)
