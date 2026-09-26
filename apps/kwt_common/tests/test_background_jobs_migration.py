"""Historical migration coverage for durable job adoption."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_pending_audio_is_adopted_without_a_broker() -> None:
    """Real migrations preserve songs, schedule unfinished work and skip failures."""
    executor = MigrationExecutor(connection)
    target = [
        ("kwt_common", "0001_initial"),
        ("player", "0028_alter_player_profile_picture_and_more"),
    ]
    try:
        executor.migrate(target)
        old = executor.loader.project_state(target).apps
        songs = old.get_model("player", "CachedSong")
        pending = songs.objects.create(
            spotify_url="https://open.spotify.com/track/pending", status="downloading"
        )
        failed = songs.objects.create(
            spotify_url="https://open.spotify.com/track/failed", status="failed"
        )
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        jobs = current.get_model("kwt_common", "BackgroundJob")
        assert jobs.objects.filter(args=[str(pending.pk)], queue="media").exists()
        assert not jobs.objects.filter(args=[str(failed.pk)]).exists()
        assert (
            current
            .get_model("player", "CachedSong")
            .objects.filter(pk=pending.pk)
            .exists()
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_successor_deadline_migration_preserves_existing_work() -> None:
    """Adding deadline storage preserves pending, leased and completed job state."""
    target = [("kwt_common", "0002_recover_pending_work")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(target)
        old = executor.loader.project_state(target).apps
        jobs = old.get_model("kwt_common", "BackgroundJob")
        due = timezone.now()
        rows = [
            jobs.objects.create(
                key=f"deadline-{state}",
                task="test.work",
                args=[state],
                due_at=due if state != "completed" else None,
                attempts=1 if state == "leased" else 0,
                completed_generation=1 if state == "completed" else 0,
            )
            for state in ("pending", "leased", "completed")
        ]
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        for row in rows:
            migrated = current.get_model("kwt_common", "BackgroundJob").objects.get(
                pk=row.pk
            )
            assert migrated.args == row.args
            assert migrated.due_at == row.due_at
            assert migrated.attempts == row.attempts
            assert migrated.completed_generation == row.completed_generation
            assert migrated.next_due_at is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
