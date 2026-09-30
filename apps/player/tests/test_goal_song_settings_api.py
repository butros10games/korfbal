"""Tests for the current-player goal-song settings endpoint."""

from __future__ import annotations

from http import HTTPStatus
import json
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.files.base import ContentFile
from django.http import HttpResponse
from django.test import Client, override_settings
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.goal_song import (
    LEGACY_GOAL_SONG_URI_DETAIL,
    MAX_GOAL_SONG_START_SECONDS,
    ParsedGoalSongPatchPayload,
    apply_goal_song_selection,
)
from apps.player.services.player_queries import player_detail_queryset
from apps.player.services.player_songs import (
    MAX_SOURCE_SECONDS,
    apply_goal_song_settings,
)


pytestmark = pytest.mark.postgres_parity


SONG_A_START_TIME_SECONDS = 12
LEGACY_START_SECONDS = 9
CONCURRENT_START_SECONDS = 20


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_requires_authentication(client: Client) -> None:
    """The goal-song settings endpoint is authenticated."""
    response = client.patch(
        "/api/player/me/goal-song/",
        data=json.dumps({"goal_song_uri": "https://example.invalid/x.mp3"}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["Content-Type"].startswith("application/json")


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_song_rejects_non_object_json_payload(client: Client) -> None:
    """The endpoint should reject non-object JSON bodies (e.g. arrays)."""
    user = get_user_model().objects.create_user(
        username="goal_song_non_object",
        password="pass1234",  # nosec
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
        password="pass1234",  # nosec
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
        password="pass1234",  # nosec
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
        password="pass1234",  # nosec
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
        password="pass1234",  # nosec
    )
    player: Player = user.player
    prior_song = PlayerSong.objects.create(player=player)
    player.goal_song_song_ids = [str(prior_song.pk)]
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


GOAL_SONG_URL = "/api/player/me/goal-song/"


def _patch_goal_song(client: Client, payload: object) -> HttpResponse:
    return client.patch(
        GOAL_SONG_URL,
        data=json.dumps(payload),
        content_type="application/json",
    )


def _selected_song(user: AbstractBaseUser, *, start: int = 5) -> PlayerSong:
    player: Player = user.player
    song = PlayerSong.objects.create(
        player=player,
        status=PlayerSongStatus.READY,
        start_time_seconds=start,
        audio_file="player_songs/synthetic-selected.mp3",
    )
    player.goal_song_song_ids = [str(song.id_uuid)]
    player.goal_song_uri = song.audio_file.url
    player.song_start_time = start
    player.save(
        update_fields=["goal_song_song_ids", "goal_song_uri", "song_start_time"]
    )
    return song


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        ([], "song_start_time must be a number or null"),
        ([12], "song_start_time must be a number or null"),
        ({}, "song_start_time must be a number or null"),
        ({"seconds": 12}, "song_start_time must be a number or null"),
        (False, "song_start_time must be a number or null"),
        ("twelve", "song_start_time must be a number or null"),
        ("nan", "song_start_time must be a finite number"),
        ("inf", "song_start_time must be a finite number"),
        ("-Infinity", "song_start_time must be a finite number"),
        ("1e309", "song_start_time must be a finite number"),
        ("1e18", "song_start_time must be at most 899"),
        (2**63, "song_start_time must be at most 899"),
        (10**400, "song_start_time must be at most 899"),
        (900, "song_start_time must be at most 899"),
    ],
)
def test_goal_song_rejects_malformed_start_times(
    client: Client, raw: object, detail: str
) -> None:
    """Malformed numbers are controlled 400s and never change saved settings."""
    user = get_user_model().objects.create_user(username="goal_song_malformed")
    song = _selected_song(user)
    client.force_login(user)

    response = _patch_goal_song(client, {"song_start_time": raw})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == detail
    song.refresh_from_db()
    user.player.refresh_from_db()
    assert (song.start_time_seconds, user.player.song_start_time) == (5, 5)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(12, 12), (12.9, 12), ("12", 12), (" 7.5 ", 7), (-3, 0), ("-3", 0), (899, 899)],
)
def test_goal_song_legacy_start_time_moves_the_selected_clip(
    client: Client, raw: object, expected: int
) -> None:
    """Legacy start times keep their parsing and change the clip that is played."""
    user = get_user_model().objects.create_user(username="goal_song_legacy_start")
    song = _selected_song(user)
    client.force_login(user)

    response = _patch_goal_song(client, {"song_start_time": raw})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["song_start_time"] == expected
    # The response describes the clip that is now played, not the prefetched one.
    (played,) = response.json()["goal_song_songs"]
    assert played["start_time_seconds"] == expected
    assert parse_qs(urlsplit(played["audio_url"]).query)["start"] == [str(expected)]
    song.refresh_from_db()
    user.player.refresh_from_db()
    assert song.start_time_seconds == expected
    assert user.player.song_start_time == expected
    assert BackgroundJob.objects.filter(
        key=f"apps.player.tasks.download_player_song:{song.id_uuid}"
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("raw", [None, ""])
def test_goal_song_blank_start_time_keeps_the_selected_clip(
    client: Client, raw: object
) -> None:
    """Older clients sent null alongside other fields; that is not a reset."""
    user = get_user_model().objects.create_user(username="goal_song_blank_start")
    song = _selected_song(user, start=8)
    client.force_login(user)

    response = _patch_goal_song(client, {"song_start_time": raw})

    assert response.status_code == HTTPStatus.OK
    song.refresh_from_db()
    user.player.refresh_from_db()
    assert (song.start_time_seconds, user.player.song_start_time) == (8, 8)


@pytest.mark.django_db
def test_goal_song_start_time_without_selection_stores_nothing(client: Client) -> None:
    """Without a selected song there is no clip, so no divergent mirror is kept."""
    user = get_user_model().objects.create_user(username="goal_song_no_selection")
    client.force_login(user)

    response = _patch_goal_song(client, {"song_start_time": 30})

    assert response.status_code == HTTPStatus.OK
    user.player.refresh_from_db()
    assert user.player.song_start_time is None
    assert user.player.goal_song_song_ids == []


@pytest.mark.django_db
def test_goal_song_legacy_uri_round_trip_and_clear_keep_the_selection(
    client: Client,
) -> None:
    """Older clients echo or blank the mirrored URI; neither diverges or clears."""
    user = get_user_model().objects.create_user(username="goal_song_legacy_uri")
    song = _selected_song(user)
    client.force_login(user)
    current_uri = user.player.goal_song_uri

    for uri in (current_uri, None, ""):
        response = _patch_goal_song(
            client, {"goal_song_uri": uri, "song_start_time": 9}
        )
        assert response.status_code == HTTPStatus.OK
        user.player.refresh_from_db()
        assert user.player.goal_song_song_ids == [str(song.id_uuid)]
        assert user.player.goal_song_uri == current_uri
        assert user.player.song_start_time == LEGACY_START_SECONDS


@pytest.mark.django_db
def test_goal_song_rejects_legacy_uri_outside_the_selection(client: Client) -> None:
    """A new legacy URI cannot point playback away from the selected songs."""
    user = get_user_model().objects.create_user(username="goal_song_foreign_uri")
    song = _selected_song(user)
    client.force_login(user)
    current_uri = user.player.goal_song_uri

    response = _patch_goal_song(
        client,
        {
            "goal_song_uri": "spotify:track:synthetic",
            "song_start_time": 20,
            "goal_song_song_ids": [str(song.id_uuid)],
        },
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == LEGACY_GOAL_SONG_URI_DETAIL
    song.refresh_from_db()
    user.player.refresh_from_db()
    assert user.player.goal_song_uri == current_uri
    assert (song.start_time_seconds, user.player.song_start_time) == (5, 5)


def test_goal_song_start_limit_matches_the_clip_source_limit() -> None:
    """Legacy start times accept exactly the starts the clip service accepts."""
    assert MAX_GOAL_SONG_START_SECONDS == MAX_SOURCE_SECONDS - 1


class _RecordedJobs:
    def __init__(self) -> None:
        self.player_songs: list[str] = []

    def cached_song(self, song_id: str) -> None:
        raise AssertionError(song_id)

    def player_song(self, song_id: str) -> None:
        self.player_songs.append(song_id)


@pytest.mark.django_db
def test_goal_song_start_time_follows_a_selection_changed_by_another_request() -> None:
    """A request holding a stale profile must edit the song that is selected now."""
    user = get_user_model().objects.create_user(username="goal_song_stale")
    first = _selected_song(user, start=5)
    stale = player_detail_queryset().get(pk=user.player.pk)
    assert stale.goal_song_song_ids == [str(first.id_uuid)]

    # Another request replaces the selection after the stale profile was loaded.
    second = PlayerSong.objects.create(
        player=user.player,
        status=PlayerSongStatus.READY,
        start_time_seconds=8,
        audio_file="player_songs/synthetic-second.mp3",
    )
    current = Player.objects.get(pk=user.player.pk)
    current.save(
        update_fields=apply_goal_song_selection(
            player=current, ids=[str(second.id_uuid)], ordered=[second]
        )
    )

    jobs = _RecordedJobs()
    apply_goal_song_settings(
        player=stale,
        settings=ParsedGoalSongPatchPayload(
            goal_song_uri_provided=False,
            goal_song_uri=None,
            song_start_time_provided=True,
            song_start_time=CONCURRENT_START_SECONDS,
            goal_song_ids_provided=False,
            goal_song_song_ids=None,
        ),
        jobs=jobs,
    )

    first.refresh_from_db()
    second.refresh_from_db()
    current.refresh_from_db()
    assert (first.start_time_seconds, second.start_time_seconds) == (
        5,
        CONCURRENT_START_SECONDS,
    )
    assert current.song_start_time == CONCURRENT_START_SECONDS
    assert current.goal_song_song_ids == [str(second.id_uuid)]
    assert jobs.player_songs == [str(second.id_uuid)]
