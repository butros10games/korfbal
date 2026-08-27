"""Regression coverage for the state-only MVP model move."""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest


MVP_TABLES = {"schedule_matchmvp", "schedule_matchmvpvote"}


@pytest.mark.django_db(transaction=True)
def test_removing_legacy_schedule_state_preserves_mvp_tables() -> None:
    """The schedule state cleanup must never delete awards-owned MVP data."""
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([
            ("awards", "0001_initial"),
            ("schedule", "0006_seasonpool_match_pool"),
        ])
        assert set(connection.introspection.table_names()) >= MVP_TABLES

        executor = MigrationExecutor(connection)
        executor.migrate([("schedule", "0007_remove_legacy_mvp_state")])

        assert set(connection.introspection.table_names()) >= MVP_TABLES
        migrated_apps = executor.loader.project_state([
            ("schedule", "0007_remove_legacy_mvp_state")
        ]).apps
        with pytest.raises(LookupError):
            migrated_apps.get_model("schedule", "MatchMvp")
        assert (
            migrated_apps.get_model("awards", "MatchMvp")._meta.db_table in MVP_TABLES
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
