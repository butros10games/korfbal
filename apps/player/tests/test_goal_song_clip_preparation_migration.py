"""Regression tests for the one-time goal-song clip preparation pass."""

from __future__ import annotations

from importlib import import_module
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
import pytest

from apps.player.models import PlayerSong, PlayerSongStatus
from apps.player.tasks import download_player_song


migration = import_module(
    "apps.player.migrations.0020_enqueue_goal_song_clip_preparation"
)


@pytest.mark.django_db
def test_backfill_queues_only_ready_direct_uploads() -> None:
    """Legacy uploads should be handed to the existing Celery preparation task."""
    user = get_user_model().objects.create_user(username="prepare-legacy-goal-song")
    ready = PlayerSong.objects.create(
        player=user.player,
        status=PlayerSongStatus.READY,
        audio_file="goal_songs/player/legacy.mp3",
    )
    PlayerSong.objects.create(
        player=user.player,
        status=PlayerSongStatus.FAILED,
        audio_file="goal_songs/player/failed.mp3",
    )
    PlayerSong.objects.create(
        player=user.player,
        status=PlayerSongStatus.READY,
        audio_file="",
    )

    with patch.object(migration.current_app, "send_task") as send_task:
        migration.enqueue_goal_song_clip_preparation(django_apps, None)

    send_task.assert_called_once_with(
        migration.PREPARE_TASK_NAME,
        args=[str(ready.id_uuid)],
    )


@pytest.mark.django_db
def test_existing_celery_task_prepares_ready_direct_upload() -> None:
    """The backwards-compatible task must materialize a migrated song's clip."""
    user = get_user_model().objects.create_user(username="prepare-goal-song-task")
    song = PlayerSong.objects.create(
        player=user.player,
        status=PlayerSongStatus.READY,
        audio_file="goal_songs/player/legacy.mp3",
    )

    with patch("apps.player.tasks.prepare_player_song_clip") as prepare:
        result = download_player_song.apply(args=[str(song.id_uuid)])

    assert result.successful()
    prepared_song = prepare.call_args.args[0]
    assert prepared_song.id_uuid == song.id_uuid
