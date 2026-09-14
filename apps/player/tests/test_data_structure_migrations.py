"""Historical schema tests for roster and ordered song migration boundaries."""

from datetime import date
from uuid import uuid4

from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
import pytest


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_rosters_and_song_order_survive_migration_and_rollback() -> None:
    """Real migrations preserve native identities, roles and ordered selections."""
    executor = MigrationExecutor(connection)
    old_targets = [
        ("player", "0025_player_archived_at"),
        ("team", "0009_remove_teamdata_team_teamda_team_id_f44f17_idx"),
    ]
    try:
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        player = old.get_model("player", "Player").objects.create(
            name="Synthetic person"
        )
        song_model = old.get_model("player", "PlayerSong")
        first = song_model.objects.create(player=player)
        first.audio_file.save("historical.mp3", ContentFile(b"ID3"), save=True)
        second = song_model.objects.create(player=player)
        player.goal_song_song_ids = [
            str(second.pk),
            str(uuid4()),
            str(first.pk),
            str(second.pk),
        ]
        player.save()
        club = old.get_model("club", "Club").objects.create(name="Synthetic club")
        team = old.get_model("team", "Team").objects.create(club=club, name="1")
        season = old.get_model("schedule", "Season").objects.create(
            name="Synthetic season",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        roster = old.get_model("team", "TeamData").objects.create(
            team=team,
            season=season,
            fallback_goal_song_song_ids=[str(first.pk), str(second.pk)],
        )
        roster.players.add(player)
        roster.coach.add(player)
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        selections = (
            current
            .get_model("player", "PlayerGoalSongSelection")
            .objects.filter(player_id=player.pk)
            .order_by("position")
        )
        songs = current.get_model("player", "PlayerSong")
        assert songs.objects.get(pk=first.pk).player_id == player.pk
        assert songs.objects.get(pk=first.pk).team_data_id is None
        team_song = songs.objects.create(team_data_id=roster.pk)
        assert team_song.player_id is None
        with pytest.raises(IntegrityError), transaction.atomic():
            songs.objects.create()
        with pytest.raises(IntegrityError), transaction.atomic():
            songs.objects.create(player_id=player.pk, team_data_id=roster.pk)
        team_song.delete()
        assert list(selections.values_list("song_id", flat=True)) == [
            second.pk,
            first.pk,
        ]
        history = current.get_model("team", "TeamRosterMembership").objects.filter(
            team_data_id=roster.pk, player_id=player.pk
        )
        assert set(history.values_list("role", flat=True)) == {"players", "coach"}
        assert all(
            value is None for value in history.values_list("started_at", flat=True)
        )
        executor = MigrationExecutor(connection)
        executor.migrate(old_targets)
        restored = executor.loader.project_state(old_targets).apps
        assert restored.get_model("player", "Player").objects.get(
            pk=player.pk
        ).goal_song_song_ids == [str(second.pk), str(first.pk)]
        assert restored.get_model("team", "TeamData").objects.get(
            pk=roster.pk
        ).fallback_goal_song_song_ids == [str(first.pk), str(second.pk)]
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_multiple_clips_preserve_existing_sources_and_selections() -> None:
    """Existing clips keep their identity while an owner may reuse cached audio."""
    executor = MigrationExecutor(connection)
    before = [("player", "0029_team_song_ownership")]
    original_start, default_duration = 12, 8
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        player = old.get_model("player", "Player").objects.create(name="Clip owner")
        cached = old.get_model("player", "CachedSong").objects.create(
            spotify_url="https://www.youtube.com/watch?v=BaW_jenozKc",
        )
        original = old.get_model("player", "PlayerSong").objects.create(
            player=player, cached_song=cached, start_time_seconds=original_start
        )
        selection = old.get_model("player", "PlayerGoalSongSelection").objects.create(
            player=player, song=original, position=0
        )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        songs = current.get_model("player", "PlayerSong")
        migrated = songs.objects.get(pk=original.pk)
        assert not migrated.clip_name
        assert migrated.clip_duration_seconds == default_duration
        assert migrated.start_time_seconds == original_start
        assert migrated.cached_song_id == cached.pk
        assert (
            current
            .get_model("player", "PlayerGoalSongSelection")
            .objects.get(pk=selection.pk)
            .song_id
            == original.pk
        )
        second = songs.objects.create(
            player_id=player.pk,
            cached_song_id=cached.pk,
            clip_name="Refrein",
            start_time_seconds=30,
            clip_duration_seconds=6,
        )
        assert second.pk != original.pk
        with pytest.raises(IntegrityError), transaction.atomic():
            songs.objects.create(player_id=player.pk, clip_duration_seconds=0)
        with pytest.raises(IntegrityError), transaction.atomic():
            songs.objects.create(player_id=player.pk, clip_duration_seconds=16)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
