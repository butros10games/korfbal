"""The retired 'private' visibility migrates to the 'club' behaviour it had."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest


BEFORE = [("player", "0032_match_live_activity")]
AFTER = [("player", "0033_retire_private_visibility")]
FIELDS = ("profile_picture_visibility", "stats_visibility", "teams_visibility")


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_private_visibility_becomes_club_for_every_field_and_player() -> None:
    """Active and archived rows are rewritten; public and club values are kept."""
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(BEFORE)
        old = executor.loader.project_state(BEFORE).apps
        player_model = old.get_model("player", "Player")
        private = player_model.objects.create(
            name="Synthetic private", **dict.fromkeys(FIELDS, "private")
        )
        archived = player_model.objects.create(
            name="",
            archived_at=timezone.now(),
            **dict.fromkeys(FIELDS, "private"),
        )
        mixed = player_model.objects.create(
            name="Synthetic mixed",
            profile_picture_visibility="public",
            stats_visibility="club",
            teams_visibility="private",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        current = executor.loader.project_state(AFTER).apps.get_model(
            "player", "Player"
        )
        rows = {row["pk"]: row for row in current._base_manager.values("pk", *FIELDS)}

        assert {field: rows[private.pk][field] for field in FIELDS} == dict.fromkeys(
            FIELDS, "club"
        )
        assert {field: rows[archived.pk][field] for field in FIELDS} == dict.fromkeys(
            FIELDS, "club"
        )
        assert {field: rows[mixed.pk][field] for field in FIELDS} == {
            "profile_picture_visibility": "public",
            "stats_visibility": "club",
            "teams_visibility": "club",
        }
        for field in FIELDS:
            assert not current._base_manager.filter(**{field: "private"}).exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
