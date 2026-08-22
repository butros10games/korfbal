"""Regression tests for the legacy goal-song data backfill."""

from __future__ import annotations

from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
import pytest

from apps.player.models import PlayerSong, PlayerSongStatus


migration = import_module("apps.player.migrations.0019_backfill_legacy_goal_songs")
LEGACY_START_TIME_SECONDS = 12


@pytest.mark.django_db
def test_backfill_creates_and_selects_modern_song_without_copying_file() -> None:
    """The new row should reference the existing storage object and be idempotent."""
    user = get_user_model().objects.create_user(username="legacy-goal-song")
    player = user.player
    player.goal_song_uri = (
        "https://media.example.test/media/goal_songs/player/legacy%20goal.mp3"
        "?signature=ignored"
    )
    player.song_start_time = LEGACY_START_TIME_SECONDS
    player.save(update_fields=["goal_song_uri", "song_start_time"])

    migration.backfill_legacy_goal_songs(django_apps, None)
    migration.backfill_legacy_goal_songs(django_apps, None)

    player.refresh_from_db()
    songs = PlayerSong.objects.filter(player=player)
    assert songs.count() == 1
    song = songs.get()
    assert player.goal_song_song_ids == [str(song.id_uuid)]
    assert song.audio_file.name == "goal_songs/player/legacy goal.mp3"
    assert song.start_time_seconds == LEGACY_START_TIME_SECONDS
    assert song.status == PlayerSongStatus.READY


@pytest.mark.django_db
def test_backfill_leaves_modern_and_non_file_configurations_unchanged() -> None:
    """Existing selections and non-storage URIs must not create duplicate rows."""
    modern_user = get_user_model().objects.create_user(username="modern-goal-song")
    modern_player = modern_user.player
    modern_song = PlayerSong.objects.create(
        player=modern_player,
        status=PlayerSongStatus.READY,
        audio_file="player_songs/modern.mp3",
    )
    modern_player.goal_song_song_ids = [str(modern_song.id_uuid)]
    modern_player.goal_song_uri = "https://media.example.test/goal_songs/old.mp3"
    modern_player.save(update_fields=["goal_song_song_ids", "goal_song_uri"])

    external_user = get_user_model().objects.create_user(username="external-goal-song")
    external_player = external_user.player
    external_player.goal_song_uri = "spotify:track:not-a-storage-file"
    external_player.save(update_fields=["goal_song_uri"])

    migration.backfill_legacy_goal_songs(django_apps, None)

    modern_player.refresh_from_db()
    external_player.refresh_from_db()
    assert modern_player.goal_song_song_ids == [str(modern_song.id_uuid)]
    assert PlayerSong.objects.filter(player=modern_player).count() == 1
    assert external_player.goal_song_song_ids == []
    assert not PlayerSong.objects.filter(player=external_player).exists()
