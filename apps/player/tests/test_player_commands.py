# ruff: noqa: D103
"""Direct contract tests for player application commands."""

from __future__ import annotations

from unittest.mock import Mock

from django.db import transaction
from django.test import override_settings
import pytest

from apps.game_tracker.tests.tracker_test_helpers import (
    OnCommitCapture,
    create_tracker_player,
)
from apps.player.application.ports import JobDispatchUnavailableError
from apps.player.models import Player, PlayerPushSubscription, PlayerSong
from apps.player.models.cached_song import CachedSongStatus
from apps.player.models.player_song import PlayerSongStatus
from apps.player.services.player_settings import (
    player_privacy_settings,
    update_player_privacy_settings,
)
from apps.player.services.player_songs import (
    PlayerSongNotFoundError,
    PlayerSongSettingsPatch,
    create_player_song,
    delete_owned_player_song,
    retry_owned_player_song_download,
    update_owned_player_song_settings,
)
from apps.player.services.push_notifications import send_test_push_notification
from apps.player.services.push_subscriptions import (
    PushSubscriptionNotFoundError,
    deactivate_push_subscription,
    register_push_subscription,
)


def _create_song_then_rollback(*, player: Player, jobs: Mock) -> None:
    with transaction.atomic():
        create_player_song(
            player=player,
            uploaded_audio=None,
            spotify_url="https://open.spotify.com/track/rollback-boundary",
            jobs=jobs,
        )
        raise RuntimeError("rollback")


@pytest.mark.django_db
def test_push_subscription_commands_preserve_endpoint_ownership() -> None:
    first_player = create_tracker_player(username="push-command-first")
    second_player = create_tracker_player(username="push-command-second")
    payload = {
        "endpoint": "https://example.com/push/command",
        "keys": {"p256dh": "abc", "auth": "def"},
    }

    created = register_push_subscription(
        user_id=first_player.user_id,
        subscription=payload,
        platform="web",
        user_agent="first",
    )
    refreshed = register_push_subscription(
        user_id=second_player.user_id,
        subscription=payload,
        platform="web",
        user_agent="second",
    )

    assert created.created is True
    assert refreshed.created is False
    assert refreshed.subscription.user_id == second_player.user_id
    assert refreshed.subscription.user_agent == "second"

    with pytest.raises(PushSubscriptionNotFoundError):
        deactivate_push_subscription(
            user_id=first_player.user_id,
            endpoint=payload["endpoint"],
            subscription_id=None,
        )

    deactivate_push_subscription(
        user_id=second_player.user_id,
        endpoint=payload["endpoint"],
        subscription_id=None,
    )
    refreshed.subscription.refresh_from_db()
    assert refreshed.subscription.is_active is False


@pytest.mark.django_db
def test_privacy_command_updates_only_supplied_settings() -> None:
    player = create_tracker_player(username="privacy-command")

    update_player_privacy_settings(
        player=player,
        changes={"stats_visibility": Player.Visibility.CLUB},
    )

    player.refresh_from_db()
    assert player_privacy_settings(player) == {
        "profile_picture_visibility": Player.Visibility.PUBLIC,
        "stats_visibility": Player.Visibility.CLUB,
        "teams_visibility": Player.Visibility.PUBLIC,
    }


@pytest.mark.django_db
def test_owned_song_command_rejects_another_players_song() -> None:
    owner = create_tracker_player(username="song-command-owner")
    other = create_tracker_player(username="song-command-other")
    song = PlayerSong.objects.create(player=owner)

    with pytest.raises(PlayerSongNotFoundError):
        delete_owned_player_song(player=other, song_id=str(song.id_uuid))

    assert PlayerSong.objects.filter(id_uuid=song.id_uuid).exists()


@pytest.mark.django_db
def test_song_creation_dispatches_only_after_commit(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    player = create_tracker_player(username="song-command-commit")
    jobs = Mock()

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        creation = create_player_song(
            player=player,
            uploaded_audio=None,
            spotify_url="https://open.spotify.com/track/commit-boundary",
            jobs=jobs,
        )
        jobs.cached_song.assert_not_called()

    jobs.cached_song.assert_called_once_with(str(creation.song.cached_song_id))


@pytest.mark.django_db
def test_song_creation_rollback_does_not_dispatch(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    player = create_tracker_player(username="song-command-rollback")
    jobs = Mock()

    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(
            RuntimeError,
            match="rollback",
        ),
    ):
        _create_song_then_rollback(player=player, jobs=jobs)

    jobs.cached_song.assert_not_called()
    assert not PlayerSong.objects.filter(player=player).exists()


@pytest.mark.django_db
def test_song_settings_command_syncs_selected_start_time_after_commit(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    updated_start_seconds = 19
    player = create_tracker_player(username="song-settings-command")
    song = PlayerSong.objects.create(player=player)
    player.goal_song_song_ids = [str(song.id_uuid)]
    player.save(update_fields=["goal_song_song_ids"])
    jobs = Mock()

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        updated = update_owned_player_song_settings(
            player=player,
            song_id=str(song.id_uuid),
            settings=PlayerSongSettingsPatch(
                start_time_seconds=updated_start_seconds,
                playback_speed=1.2,
            ),
            jobs=jobs,
        )
        jobs.player_song.assert_not_called()

    jobs.player_song.assert_called_once_with(str(song.id_uuid))
    updated.refresh_from_db()
    player.refresh_from_db()
    assert updated.start_time_seconds == updated_start_seconds
    assert updated.playback_speed == pytest.approx(1.2)
    assert player.song_start_time == updated_start_seconds


@pytest.mark.django_db
def test_playback_only_song_update_does_not_regenerate_audio() -> None:
    player = create_tracker_player(username="song-playback-only-command")
    song = PlayerSong.objects.create(player=player)
    jobs = Mock()

    updated = update_owned_player_song_settings(
        player=player,
        song_id=str(song.id_uuid),
        settings=PlayerSongSettingsPatch(playback_speed=1.3),
        jobs=jobs,
    )

    jobs.player_song.assert_not_called()
    updated.refresh_from_db()
    assert updated.playback_speed == pytest.approx(1.3)


@pytest.mark.django_db
def test_song_creation_marks_failed_when_dispatch_is_unavailable(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    player = create_tracker_player(username="song-command-broker-failure")
    jobs = Mock()
    jobs.cached_song.side_effect = JobDispatchUnavailableError

    with django_capture_on_commit_callbacks(execute=True):
        creation = create_player_song(
            player=player,
            uploaded_audio=None,
            spotify_url="https://open.spotify.com/track/broker-failure",
            jobs=jobs,
        )

    cached = creation.song.cached_song
    assert cached is not None
    cached.refresh_from_db()
    assert cached.status == CachedSongStatus.FAILED
    assert cached.error_message == "Celery broker unavailable"


@pytest.mark.django_db
def test_retry_command_commits_queued_state_before_dispatch(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    player = create_tracker_player(username="song-retry-command")
    song = PlayerSong.objects.create(
        player=player,
        status=PlayerSongStatus.FAILED,
        error_message="failed",
    )
    jobs = Mock()

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        retried = retry_owned_player_song_download(
            player=player,
            song_id=str(song.id_uuid),
            jobs=jobs,
        )
        jobs.player_song.assert_not_called()
        assert retried.status == PlayerSongStatus.QUEUED
        assert not retried.error_message

    jobs.player_song.assert_called_once_with(str(song.id_uuid))


@pytest.mark.django_db
@override_settings(
    WEBPUSH_VAPID_PUBLIC_KEY="public",
    WEBPUSH_VAPID_PRIVATE_KEY="private",
    WEBPUSH_VAPID_SUBJECT="mailto:test@example.com",
)
def test_test_push_command_owns_provider_orchestration() -> None:
    player = create_tracker_player(username="test-push-command")
    subscription = PlayerPushSubscription.objects.create(
        user_id=player.user_id,
        endpoint="https://example.com/push/test-command",
        subscription={
            "endpoint": "https://example.com/push/test-command",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
    )
    send_pushes = Mock(return_value=(1, 0, []))

    result = send_test_push_notification(
        user_id=player.user_id,
        webpush_available=lambda: True,
        send_pushes=send_pushes,
    )

    assert result.total == 1
    assert result.sent == 1
    assert result.failed == 0
    assert send_pushes.call_args.kwargs["subs"] == [subscription]
    assert send_pushes.call_args.kwargs["payload"].title == "Test pushmelding"
