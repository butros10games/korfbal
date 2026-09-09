"""Migration regressions for tracker command reconciliation."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest

from apps.game_tracker.models import TrackerCommand
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.migration_regression
@pytest.mark.slow_migration
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


@pytest.mark.django_db(transaction=True)
@pytest.mark.migration_regression
@pytest.mark.slow_migration
@pytest.mark.parametrize(
    "first_branch",
    [
        "0038_tracker_access_link",
        "0038_remove_playermatchimpact_uniq_player_match_impact_and_more",
    ],
)
def test_access_and_integrity_merge_preserves_both_upgrade_paths(
    first_branch: str,
) -> None:
    """Either branch can upgrade without losing scores, links or impact rows."""
    tracker = create_tracker_match(prefix="Branch merge")
    player = create_tracker_player(username="branch-merge-player")
    executor = MigrationExecutor(connection)
    try:
        baseline = [("game_tracker", "0037_alter_matchdata_score_source")]
        executor.migrate(baseline)
        old_apps = executor.loader.project_state(baseline).apps
        old_match_data = old_apps.get_model("game_tracker", "MatchData")
        scores = {"home_score": 9, "away_score": 8}
        old_match_data.objects.filter(pk=tracker.match_data.pk).update(**scores)
        impact = old_apps.get_model("game_tracker", "PlayerMatchImpact").objects.create(
            match_data_id=tracker.match_data.pk,
            player_id=player.pk,
            algorithm_version="v1",
        )

        executor = MigrationExecutor(connection)
        branch = [("game_tracker", first_branch)]
        executor.migrate(branch)
        branch_apps = executor.loader.project_state(branch).apps
        link_values = {
            "match_id": tracker.match.pk,
            "team_id": tracker.home_team.pk,
            "token_hash": "synthetic-hash",
            "expires_at": timezone.now() + timedelta(hours=1),
        }
        if first_branch == "0038_tracker_access_link":
            branch_apps.get_model("game_tracker", "TrackerAccessLink").objects.create(
                **link_values
            )
        else:
            branch_apps.get_model("game_tracker", "PlayerMatchImpact").objects.filter(
                pk=impact.pk
            ).update(source_revision=7)

        executor = MigrationExecutor(connection)
        merged = [("game_tracker", "0039_merge_access_and_integrity")]
        executor.migrate(merged)
        apps = executor.loader.project_state(merged).apps
        match_data = apps.get_model("game_tracker", "MatchData")
        assert (
            match_data.objects
            .filter(pk=tracker.match_data.pk)
            .values("home_score", "away_score")
            .get()
            == scores
        )
        access_link = apps.get_model("game_tracker", "TrackerAccessLink")
        link, created = access_link.objects.get_or_create(
            match_id=tracker.match.pk,
            team_id=tracker.home_team.pk,
            defaults=link_values,
        )
        assert created is (first_branch != "0038_tracker_access_link")
        assert link.token_hash == link_values["token_hash"]
        migrated_impact = apps.get_model("game_tracker", "PlayerMatchImpact")
        expected_revision = None if first_branch == "0038_tracker_access_link" else 7
        assert (
            migrated_impact.objects.get(pk=impact.pk).source_revision
            == expected_revision
        )
        migrated_impact.objects.create(
            match_data_id=tracker.match_data.pk,
            player_id=player.pk,
            algorithm_version="v2",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            access_link.objects.create(**link_values)
        with pytest.raises(IntegrityError), transaction.atomic():
            match_data.objects.filter(pk=tracker.match_data.pk).update(home_score=-1)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
