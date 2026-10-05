"""Preserve imported identities and older writers across schema expansion."""

from datetime import UTC, date, datetime

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.migration_regression]


def test_import_provenance_expansion_preserves_results_and_legacy_writes() -> None:
    """New metadata defaults must not rewrite history or reject old importers."""
    before = [("competition", "0034_merge_competition_periods_history_lineup_cohort")]
    after = [("competition", "0035_imported_data_provenance")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old_apps = executor.loader.project_state(before).apps
    season = old_apps.get_model("schedule", "Season").objects.create(
        name="Synthetic provenance season",
        start_date=date(2025, 7, 1),
        end_date=date(2026, 6, 30),
    )
    old_club_model = old_apps.get_model("competition", "Club")
    old_team_model = old_apps.get_model("competition", "Team")
    old_pool_model = old_apps.get_model("competition", "Pool")
    old_match_model = old_apps.get_model("competition", "Match")
    club = old_club_model.objects.create(
        external_id="migration-club", name="Synthetic club"
    )
    home = old_team_model.objects.create(
        season=season, club=club, external_id="migration-home", name="Synthetic home"
    )
    away = old_team_model.objects.create(
        season=season, club=club, external_id="migration-away", name="Synthetic away"
    )
    pool = old_pool_model.objects.create(season=season, external_id="migration-pool")
    match = old_match_model.objects.create(
        season=season,
        external_id="migration-result",
        pool=pool,
        home_team=home,
        away_team=away,
        starts_at=datetime(2025, 9, 1, tzinfo=UTC),
        status="FINAL",
        home_score=20,
        away_score=15,
    )
    old_apps.get_model("competition", "PoolEntry").objects.create(
        pool=pool, team=home, standing={"Position": 1, "TotalPoints": -1}
    )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    new_apps = executor.loader.project_state(after).apps
    result = new_apps.get_model("competition", "Match").objects.get(pk=match.pk)
    assert (result.external_id, result.home_score, result.away_score) == (
        "migration-result",
        20,
        15,
    )
    assert result.source_context == {}
    assert result.metadata_observations == {}
    new_pool = new_apps.get_model("competition", "Pool").objects.get(pk=pool.pk)
    assert not new_pool.phase
    assert new_pool.standings_provenance == {}
    entry = new_apps.get_model("competition", "PoolEntry").objects.get(pool_id=pool.pk)
    assert entry.standing == {"Position": 1, "TotalPoints": -1}
    assert entry.computed_standing is None

    # Rolling deployments still write with the old model state. Database
    # defaults must supply every new non-null metadata column.
    older_writer = old_club_model.objects.create(
        external_id="migration-old-writer", name="Synthetic older writer"
    )
    new_club = new_apps.get_model("competition", "Club").objects.get(pk=older_writer.pk)
    assert new_club.metadata_observations == {}
    assert new_club.sport_definitions == []
    assert new_club.current_directory_observed_at is None
