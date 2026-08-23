"""Migration regressions for tracker command reconciliation."""

from __future__ import annotations

from uuid import uuid4

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest

from apps.game_tracker.models import TrackerCommand
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


@pytest.mark.django_db(transaction=True)
def test_global_command_id_migration_rekeys_cross_match_duplicates() -> None:
    """Valid historical composite IDs cannot block the global unique upgrade."""
    first = create_tracker_match(prefix="Command migration A")
    second = create_tracker_match(prefix="Command migration B")
    preserved_command_id = uuid4()
    first_receipt = TrackerCommand.objects.create(
        command_id=preserved_command_id,
        match_data=first.match_data,
        team=first.home_team,
        sequence=1,
        command="new_attack",
        payload_hash="a" * 64,
    )
    second_receipt = TrackerCommand.objects.create(
        command_id=uuid4(),
        match_data=second.match_data,
        team=second.home_team,
        sequence=1,
        command="new_attack",
        payload_hash="b" * 64,
    )

    executor = MigrationExecutor(connection)
    try:
        executor.migrate([("game_tracker", "0026_canonical_event_details")])
        old_apps = executor.loader.project_state([
            ("game_tracker", "0026_canonical_event_details")
        ]).apps
        old_tracker_command = old_apps.get_model("game_tracker", "TrackerCommand")
        old_tracker_command.objects.filter(pk=second_receipt.pk).update(
            command_id=preserved_command_id
        )

        executor = MigrationExecutor(connection)
        executor.migrate([("game_tracker", "0027_tracker_command_reconciliation")])
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    migrated_ids = list(
        TrackerCommand.objects
        .filter(pk__in=[first_receipt.pk, second_receipt.pk])
        .order_by("created_at", "id_uuid")
        .values_list("command_id", flat=True)
    )
    assert migrated_ids[0] == preserved_command_id
    assert migrated_ids[1] != preserved_command_id
