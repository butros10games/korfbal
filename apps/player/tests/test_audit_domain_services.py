# ruff: noqa: D103
"""Audit tests for player application-service boundaries."""

from __future__ import annotations

import json
from unittest.mock import Mock
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
import pytest

from apps.game_tracker.tests.tracker_test_helpers import create_tracker_player
from apps.player.application.ports import CommandRunOptions, WebPushDeliveryError
from apps.player.models import CachedSong, PlayerPushSubscription, PlayerSong
from apps.player.models.cached_song import CachedSongStatus
from apps.player.models.player_song import PlayerSongStatus
from apps.player.services.audio_clipper import Mp3ClipSpec, transcode_to_mp3_clip_file
from apps.player.services.goal_song import (
    GoalSongSelectionError,
    ParsedGoalSongPatchPayload,
    update_goal_song_settings,
)
from apps.player.services.match_notifications import send_payload_to_users
from apps.player.services.player_songs import (
    PlayerSongAlreadyReadyError,
    PlayerSongClipRequest,
    create_player_song,
    resolve_player_song_clip,
    retry_owned_player_song_download,
)
from apps.player.services.song_processing import process_cached_song_download
from apps.player.services.web_push import WebPushPayload, send_to_model_subscription
from apps.player.spotify import canonicalize_spotify_track_url


EXPECTED_DISPATCH_COUNT = 2
ORIGINAL_START_SECONDS = 11


@pytest.mark.django_db
@pytest.mark.parametrize("status_code", [404, 410])
def test_dead_web_push_subscriptions_are_deactivated(status_code: int) -> None:
    player = create_tracker_player(username=f"dead-push-{status_code}")
    subscription = PlayerPushSubscription.objects.create(
        user_id=player.user_id,
        endpoint=f"https://push.example.invalid/{status_code}",
        subscription={"endpoint": f"https://push.example.invalid/{status_code}"},
    )
    client = Mock()
    client.send.side_effect = WebPushDeliveryError(
        "expired subscription",
        status_code=status_code,
    )

    send_to_model_subscription(
        sub=subscription,
        payload=WebPushPayload(title="Goal", body="Scored", url="/matches/1"),
        client=client,
        ttl_seconds=120,
    )

    subscription.refresh_from_db()
    assert subscription.is_active is False


@pytest.mark.django_db
def test_retryable_web_push_failure_propagates_without_deactivation() -> None:
    player = create_tracker_player(username="retryable-push")
    subscription = PlayerPushSubscription.objects.create(
        user_id=player.user_id,
        endpoint="https://push.example.invalid/retryable",
        subscription={"endpoint": "https://push.example.invalid/retryable"},
    )
    client = Mock()
    failure = WebPushDeliveryError("provider unavailable", status_code=503)
    client.send.side_effect = failure

    with pytest.raises(WebPushDeliveryError) as error:
        send_to_model_subscription(
            sub=subscription,
            payload=WebPushPayload(title="Goal", body="Scored", url="/matches/1"),
            client=client,
            ttl_seconds=120,
        )

    assert error.value is failure
    subscription.refresh_from_db()
    assert subscription.is_active is True


@pytest.mark.django_db
def test_match_notification_routes_active_web_and_expo_destinations() -> None:
    recipient = create_tracker_player(username="notification-recipient")
    ignored = create_tracker_player(username="notification-ignored")
    web = PlayerPushSubscription.objects.create(
        user_id=recipient.user_id,
        endpoint="https://push.example.invalid/web",
        subscription={"endpoint": "https://push.example.invalid/web"},
    )
    PlayerPushSubscription.objects.create(
        user_id=recipient.user_id,
        endpoint="ExponentPushToken[recipient]",
        subscription={"endpoint": "ExponentPushToken[recipient]"},
        platform="expo",
    )
    PlayerPushSubscription.objects.create(
        user_id=recipient.user_id,
        endpoint="https://push.example.invalid/inactive",
        subscription={"endpoint": "https://push.example.invalid/inactive"},
        is_active=False,
    )
    PlayerPushSubscription.objects.create(
        user_id=ignored.user_id,
        endpoint="https://push.example.invalid/other-user",
        subscription={"endpoint": "https://push.example.invalid/other-user"},
    )
    send_web = Mock()
    send_expo = Mock()
    payload = WebPushPayload(title="Final", body="12 - 10", url="/matches/1")

    send_payload_to_users(
        user_ids=[recipient.user_id],
        payload=payload,
        send_web_push=send_web,
        send_expo_push=send_expo,
    )

    send_web.assert_called_once_with(sub=web, payload=payload)
    send_expo.assert_called_once()
    assert send_expo.call_args.kwargs["tokens"] == ["ExponentPushToken[recipient]"]
    expo_payload = send_expo.call_args.kwargs["payload"]
    assert (expo_payload.title, expo_payload.body, expo_payload.url) == (
        "Final",
        "12 - 10",
        "/matches/1",
    )


def test_web_push_payload_serializes_only_populated_optional_fields() -> None:
    payload = WebPushPayload(
        title="Goal",
        body="Scored",
        url="/matches/1",
        tag="goal:1",
        icon="/icon.png",
        data={"score": 1},
    )

    assert json.loads(payload.to_json()) == {
        "title": "Goal",
        "body": "Scored",
        "url": "/matches/1",
        "tag": "goal:1",
        "icon": "/icon.png",
        "data": {"score": 1},
    }


def test_audio_transcoder_clamps_range_and_uses_safe_command_options() -> None:
    command_runner = Mock()

    transcode_to_mp3_clip_file(
        input_path="/input/source.wav",
        output_path="/output/clip.mp3",
        spec=Mp3ClipSpec(start_seconds=-5, duration_seconds=0),
        ffmpeg_path="/usr/bin/ffmpeg",
        command_runner=command_runner,
    )

    cmd, options = command_runner.run.call_args.args
    assert cmd == [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0",
        "-i",
        "/input/source.wav",
        "-t",
        "1",
        "-vn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "4",
        "/output/clip.mp3",
    ]
    assert options == CommandRunOptions(check=True)


def test_audio_transcoder_requires_ffmpeg() -> None:
    with pytest.raises(FileNotFoundError, match="ffmpeg not found"):
        transcode_to_mp3_clip_file(
            input_path="input",
            output_path="output",
            ffmpeg_path=None,
            command_runner=Mock(),
        )


@pytest.mark.django_db
def test_invalid_goal_song_selection_does_not_persist_partial_settings() -> None:
    player = create_tracker_player(username="goal-selection-owner")
    other = create_tracker_player(username="goal-selection-other")
    player.goal_song_uri = "https://media.example.invalid/original.mp3"
    player.song_start_time = ORIGINAL_START_SECONDS
    player.save(update_fields=["goal_song_uri", "song_start_time"])
    wrong_owner_song = PlayerSong.objects.create(
        player=other,
        status=PlayerSongStatus.READY,
        audio_file="player_songs/wrong-owner.mp3",
    )

    with pytest.raises(GoalSongSelectionError) as error:
        update_goal_song_settings(
            player=player,
            settings=ParsedGoalSongPatchPayload(
                goal_song_uri_provided=True,
                goal_song_uri="https://media.example.invalid/replacement.mp3",
                song_start_time_provided=True,
                song_start_time=22,
                goal_song_ids_provided=True,
                goal_song_song_ids=[str(wrong_owner_song.id_uuid)],
            ),
        )

    assert error.value.detail == "Unknown song id(s)"
    player.refresh_from_db()
    assert player.goal_song_uri == "https://media.example.invalid/original.mp3"
    assert player.song_start_time == ORIGINAL_START_SECONDS
    assert player.goal_song_song_ids == []


@pytest.mark.django_db(transaction=True)
def test_direct_upload_song_creation_is_ready_and_dispatches_after_commit() -> None:
    player = create_tracker_player(username="direct-upload-command")
    jobs = Mock()
    uploaded = SimpleUploadedFile(
        "../My goal song.mp3",
        b"ID3-audio",
        content_type="audio/mpeg",
    )

    with transaction.atomic():
        creation = create_player_song(
            player=player,
            uploaded_audio=uploaded,
            spotify_url=None,
            jobs=jobs,
        )
        jobs.player_song.assert_not_called()

    song = creation.song
    assert creation.created is True
    assert song.title == "My goal song"
    assert song.status == PlayerSongStatus.READY
    assert not song.spotify_url
    assert song.cached_song is None
    jobs.player_song.assert_called_once_with(str(song.id_uuid))


@pytest.mark.django_db(transaction=True)
def test_spotify_song_creation_is_idempotent_for_one_player() -> None:
    player = create_tracker_player(username="idempotent-song-command")
    jobs = Mock()
    raw_url = "https://www.open.spotify.com/intl-nl/track/track-id?si=tracking"

    first = create_player_song(
        player=player,
        uploaded_audio=None,
        spotify_url=raw_url,
        jobs=jobs,
    )
    second = create_player_song(
        player=player,
        uploaded_audio=None,
        spotify_url=raw_url,
        jobs=jobs,
    )

    assert first.created is True
    assert second.created is False
    assert second.song.id_uuid == first.song.id_uuid
    assert CachedSong.objects.count() == 1
    assert PlayerSong.objects.filter(player=player).count() == 1
    assert jobs.cached_song.call_count == EXPECTED_DISPATCH_COUNT


@pytest.mark.django_db(transaction=True)
def test_ready_song_cannot_be_retried() -> None:
    player = create_tracker_player(username="ready-retry-command")
    song = PlayerSong.objects.create(
        player=player,
        status=PlayerSongStatus.READY,
        audio_file="player_songs/ready.mp3",
    )
    jobs = Mock()

    with pytest.raises(PlayerSongAlreadyReadyError):
        retry_owned_player_song_download(
            player=player,
            song_id=str(song.id_uuid),
            jobs=jobs,
        )

    jobs.player_song.assert_not_called()


@pytest.mark.django_db
def test_clip_resolution_short_circuits_missing_or_audio_less_songs() -> None:
    player = create_tracker_player(username="clip-resolution")
    song = PlayerSong.objects.create(player=player, status=PlayerSongStatus.READY)
    prepare = Mock()
    jobs = Mock()

    assert (
        resolve_player_song_clip(
            request=PlayerSongClipRequest(
                song_id=str(uuid4()),
                start_seconds=0,
                duration_seconds=8,
                enqueue_if_missing=True,
            ),
            prepare_clip=prepare,
            jobs=jobs,
        )
        is None
    )
    assert (
        resolve_player_song_clip(
            request=PlayerSongClipRequest(
                song_id=str(song.id_uuid),
                start_seconds=0,
                duration_seconds=8,
                enqueue_if_missing=True,
            ),
            prepare_clip=prepare,
            jobs=jobs,
        )
        is None
    )
    prepare.assert_not_called()
    jobs.player_song.assert_not_called()


@pytest.mark.django_db
def test_ready_cached_song_prepares_all_dependents_without_downloading() -> None:
    first = create_tracker_player(username="ready-cache-first")
    second = create_tracker_player(username="ready-cache-second")
    cached = CachedSong.objects.create(
        spotify_url="https://open.spotify.com/track/ready-cache",
        status=CachedSongStatus.READY,
        audio_file="cached_songs/ready.mp3",
    )
    first_song = PlayerSong.objects.create(player=first, cached_song=cached)
    second_song = PlayerSong.objects.create(player=second, cached_song=cached)
    download = Mock(side_effect=AssertionError("ready cache must not download"))
    prepare = Mock()

    process_cached_song_download(
        str(cached.id_uuid),
        download_track=download,
        prepare_clip=prepare,
    )

    download.assert_not_called()
    assert {call.args[0] for call in prepare.call_args_list} == {
        first_song,
        second_song,
    }


@pytest.mark.parametrize(
    "raw",
    [
        "https://open.spotify.com/track/abc123?si=tracking#fragment",
        "https://www.open.spotify.com/intl-nl/track/abc123",
    ],
)
def test_spotify_track_urls_are_canonicalized_for_cache_identity(raw: str) -> None:
    assert canonicalize_spotify_track_url(raw) == (
        "https://open.spotify.com/track/abc123"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://open.spotify.com/track/abc123",
        "https://example.invalid/track/abc123",
        "https://open.spotify.com/album/abc123",
    ],
)
def test_invalid_spotify_track_urls_fail_before_provider_work(raw: str) -> None:
    with pytest.raises(ValueError, match="Spotify URL"):
        canonicalize_spotify_track_url(raw)
