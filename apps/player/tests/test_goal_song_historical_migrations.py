"""Run legacy goal-song backfills against their historical database schemas."""

from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest


LEGACY_START_SECONDS = 12
EXPECTED_DISPATCHES = 2


@pytest.mark.migration_regression
@pytest.mark.slow_migration
@pytest.mark.django_db(transaction=True)
def test_legacy_song_backfill_and_clip_dispatch_preserve_existing_selections() -> None:
    """Upgrade populated legacy data, then replay the reversible boundary safely."""
    before = [("player", "0018_player_date_of_birth")]
    after = [("player", "0020_enqueue_goal_song_clip_preparation")]
    executor = MigrationExecutor(connection)
    with patch("celery.current_app.send_task") as dispatch:
        try:
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            players = old.get_model("player", "Player")
            songs = old.get_model("player", "PlayerSong")
            users = old.get_model(settings.AUTH_USER_MODEL)
            legacy = players.objects.create(
                user=users.objects.create(username="legacy"),
                goal_song_uri="https://media.example.test/media/goal_songs/legacy%20goal.mp3?signature=synthetic",
                song_start_time=LEGACY_START_SECONDS,
            )
            modern = players.objects.create(
                user=users.objects.create(username="modern"),
                goal_song_uri="goal_songs/ignored.mp3",
            )
            selected = songs.objects.create(
                player=modern, audio_file="player_songs/selected.mp3", status="ready"
            )
            modern.goal_song_song_ids = [str(selected.pk)]
            modern.save()
            external = players.objects.create(
                user=users.objects.create(username="external"),
                goal_song_uri="spotify:track:external",
            )
            songs.objects.create(player=external, audio_file="", status="ready")
            songs.objects.create(
                player=external, audio_file="player_songs/failed.mp3", status="failed"
            )
            cached = old.get_model("player", "CachedSong").objects.create(
                spotify_url="https://open.spotify.com/track/synthetic"
            )
            songs.objects.create(
                player=external,
                cached_song=cached,
                audio_file="cached_songs/provider.mp3",
                status="ready",
            )
            dispatch.reset_mock()
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            migrated = executor.loader.project_state(after).apps
            migrated_players = migrated.get_model("player", "Player")
            migrated_songs = migrated.get_model("player", "PlayerSong")
            song = migrated_songs.objects.get(player_id=legacy.pk)
            assert song.audio_file.name == "goal_songs/legacy goal.mp3"
            assert song.start_time_seconds == LEGACY_START_SECONDS
            assert song.status == "ready"
            assert migrated_players.objects.get(pk=legacy.pk).goal_song_song_ids == [
                str(song.pk)
            ]
            assert migrated_players.objects.get(pk=modern.pk).goal_song_song_ids == [
                str(selected.pk)
            ]
            assert migrated_players.objects.get(pk=external.pk).goal_song_song_ids == []
            assert {call.kwargs["args"][0] for call in dispatch.call_args_list} == {
                str(song.pk),
                str(selected.pk),
            }
            assert dispatch.call_count == EXPECTED_DISPATCHES
            assert all(
                call.args == ("apps.player.tasks.download_player_song",)
                for call in dispatch.call_args_list
            )
            executor = MigrationExecutor(connection)
            executor.migrate(before)
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            assert migrated_songs.objects.filter(player_id=legacy.pk).count() == 1
            assert migrated_songs.objects.get(player_id=legacy.pk).pk == song.pk
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
