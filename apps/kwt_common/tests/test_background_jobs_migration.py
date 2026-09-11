"""Historical migration coverage for durable job adoption."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
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
