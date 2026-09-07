"""Verify existing source variants are grouped by the real migration."""

from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_existing_variants_are_backfilled() -> None:
    """Historical models exercise schema order and preserve source IDs."""
    executor = MigrationExecutor(connection)
    try:
        old_targets = [("competition", "0001_initial")]
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="group-migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        club = old.get_model("competition", "Club").objects.create(
            external_id="migration", name="Example"
        )
        team_model = old.get_model("competition", "Team")
        for source, name, sport in (
            ("ve", "Example 1", "VE"),
            ("za", " example  1 ", "ZA"),
            ("ve2", "Example 2", "VE"),
        ):
            team_model.objects.create(
                season=season, club=club, external_id=source, name=name, sport=sport
            )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        teams = current.get_model("competition", "Team").objects
        assert (
            teams.get(external_id="ve").group_id == teams.get(external_id="za").group_id
        )
        assert (
            teams.get(external_id="ve").group_id
            != teams.get(external_id="ve2").group_id
        )
        assert set(teams.values_list("external_id", flat=True)) == {"ve", "za", "ve2"}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_shared_identity_migrations_preserve_native_records() -> None:
    """Add source links and uniqueness without replacing native primary keys."""
    executor = MigrationExecutor(connection)
    old_targets = [
        (
            "competition",
            "0003_club_local_club_match_local_match_pool_local_pool_and_more",
        ),
        ("team", "0006_remove_teamdata_team_teamda_team_id_18931e_idx_and_more"),
        ("schedule", "0008_remove_match_schedule_ma_home_te_5e2ac9_idx_and_more"),
        ("game_tracker", "0035_player_match_impact_wpa"),
    ]
    try:
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        club = old.get_model("club", "Club").objects.create(
            name="Shared migration club"
        )
        team = old.get_model("team", "Team").objects.create(club=club, name="1")
        season = old.get_model("schedule", "Season").objects.create(
            name="shared migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        roster = old.get_model("team", "TeamData").objects.create(
            team=team, season=season, competition="Keep my competition", team_rank=3
        )
        source = old.get_model("competition", "Club").objects.create(
            external_id="shared-migration",
            name="Shared migration club",
            local_club_id=club.pk,
        )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        native = current.get_model("team", "TeamData").objects.get(pk=roster.pk)
        assert native.team_id == team.pk
        assert native.season_id == season.pk
        assert native.competition == "Keep my competition"
        assert (
            current
            .get_model("competition", "Club")
            .objects.get(pk=source.pk)
            .local_club_id
            == club.pk
        )
        assert {
            constraint.name
            for constraint in current.get_model("team", "TeamData")._meta.constraints
        } == {"unique_team_data_per_season"}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_logo_migration_requeues_only_club_lists() -> None:
    """Discover missing logo metadata without resetting completed match feeds."""
    executor = MigrationExecutor(connection)
    try:
        old_targets = [
            ("competition", "0004_match_local_created_match_published_at_and_more")
        ]
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="logos", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
        )
        stamp = timezone.now()
        resource = old.get_model("competition", "SyncResource")
        for kind in ("clubs", "club_results"):
            resource.objects.create(
                season=season,
                kind=kind,
                source_id="",
                fetched_at=stamp,
                next_sync_at=stamp,
                etag="old",
            )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        resource = executor.loader.project_state(targets).apps.get_model(
            "competition", "SyncResource"
        )
        assert (
            resource.objects.get(season_id=season.pk, kind="clubs").fetched_at is None
        )
        assert not resource.objects.get(season_id=season.pk, kind="clubs").etag
        assert (
            resource.objects.get(season_id=season.pk, kind="club_results").fetched_at
            == stamp
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
