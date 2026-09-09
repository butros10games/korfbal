"""Historical schema tests for roster and ordered song migration boundaries."""

from datetime import date
from uuid import uuid4

from django.db import connection
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
