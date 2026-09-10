"""Validate the shipped migration graph even in the no-migrations test lane."""

from django.db.migrations.loader import MigrationLoader
from django.test import override_settings


@override_settings(MIGRATION_MODULES={})
def test_shipped_migration_graph_has_no_conflicting_leaves() -> None:
    """Deployment must resolve every app to a single migration leaf."""
    loader = MigrationLoader(None)

    assert loader.detect_conflicts() == {}
