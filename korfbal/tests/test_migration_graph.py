"""Validate the shipped migration graph even in the no-migrations test lane."""

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings
import pytest


@override_settings(MIGRATION_MODULES={})
def test_shipped_migration_graph_has_no_conflicting_leaves() -> None:
    """Deployment must resolve every app to a single migration leaf."""
    loader = MigrationLoader(None)

    assert loader.detect_conflicts() == {}


@pytest.mark.django_db
@override_settings(MIGRATION_MODULES={})
def test_every_model_change_has_a_shipped_migration() -> None:
    """A model edit without its migration would deploy an unmigrated schema."""
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
