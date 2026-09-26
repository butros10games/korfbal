"""Historical migration coverage for field-scoped tournament permissions."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_field_role_migration_preserves_grants_until_field_deletion() -> None:
    """Migrating retains grants; deleting their field revokes only scoped grants."""
    target = [
        ("tournament", "0009_tournament_cup_rules_tournamentmatch_cup_state_and_more")
    ]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(target)
        old = executor.loader.project_state(target).apps
        users = old.get_model("auth", "User")
        owner = users.objects.create(username="migration-owner")
        tournament = old.get_model("tournament", "Tournament").objects.create(
            name="Field permissions",
            slug="field-permissions",
            owner=owner,
            starts_at=timezone.now(),
        )
        field = old.get_model("tournament", "TournamentField").objects.create(
            tournament=tournament, label="Field 1"
        )
        members = old.get_model("tournament", "TournamentMember")
        scoped = members.objects.create(
            tournament=tournament,
            user=users.objects.create(username="migration-scoped"),
            role="scorekeeper",
            field=field,
        )
        retained = [
            members.objects.create(
                tournament=tournament,
                user=users.objects.create(username=f"migration-{role}"),
                role=role,
            ).pk
            for role in ("manager", "scorekeeper")
        ]
        grants = list(members.objects.order_by("pk").values())

        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        members = current.get_model("tournament", "TournamentMember")
        assert list(members.objects.order_by("pk").values()) == grants

        current.get_model("tournament", "TournamentField").objects.filter(
            pk=field.pk
        ).delete()

        assert not members.objects.filter(pk=scoped.pk).exists()
        assert set(members.objects.values_list("pk", flat=True)) == set(retained)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
