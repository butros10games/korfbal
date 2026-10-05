"""Coverage migration preserves real fixtures in legacy flagged seasons."""

from datetime import UTC, date, datetime

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest


RECORDED_FIXTURES = 126


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_populated_legacy_gap_becomes_partial_without_losing_fixtures() -> None:
    """Both native and unpublished source data clear the legacy blocking flag."""
    before = [
        ("schedule", "0013_season_data_unavailable"),
        ("competition", "0034_merge_competition_periods_history_lineup_cohort"),
    ]
    after = [("schedule", "0014_season_data_coverage")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        seasons = old.get_model("schedule", "Season").objects
        populated, source_only, empty, unknown = [
            seasons.create(
                name=name,
                start_date=date(2024, 7, 1),
                end_date=date(2024, 12, 31),
                data_unavailable=flag,
            )
            for name, flag in (
                ("126 recorded fixtures", True),
                ("unpublished fixtures", True),
                ("tested empty discovery", True),
                ("unreviewed source coverage", False),
            )
        ]
        club = old.get_model("club", "Club").objects.create(
            name="Synthetic coverage club"
        )
        native_teams = [
            old.get_model("team", "Team").objects.create(
                club=club,
                name=str(index),
            )
            for index in range(2)
        ]
        fixture = old.get_model("schedule", "Match")
        for _ in range(RECORDED_FIXTURES):
            fixture.objects.create(
                season=populated,
                home_team=native_teams[0],
                away_team=native_teams[1],
                start_time=datetime(2024, 9, 1, tzinfo=UTC),
            )
        source_club = old.get_model("competition", "Club").objects.create(
            external_id="C-SYNTHETIC",
            name="Synthetic source club",
        )
        source_teams = [
            old.get_model("competition", "Team").objects.create(
                season=source_only,
                external_id=f"T{index}",
                club=source_club,
                name=str(index),
                sport="KORFBALL-VE-WK",
            )
            for index in range(2)
        ]
        old.get_model("competition", "Match").objects.create(
            season=source_only,
            external_id="M1",
            home_team=source_teams[0],
            away_team=source_teams[1],
            starts_at=datetime(2024, 9, 1, tzinfo=UTC),
            status="FINAL",
            home_score=8,
            away_score=7,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new = executor.loader.project_state(after).apps
        rows = new.get_model("schedule", "Season").objects
        assert (
            rows.get(pk=populated.pk).data_coverage,
            rows.get(pk=populated.pk).data_unavailable,
        ) == ("partial", False)
        assert (
            rows.get(pk=source_only.pk).data_coverage,
            rows.get(pk=source_only.pk).data_unavailable,
        ) == ("partial", False)
        assert (
            rows.get(pk=empty.pk).data_coverage,
            rows.get(pk=empty.pk).data_unavailable,
        ) == ("unavailable", True)
        assert rows.get(pk=unknown.pk).data_coverage == "unknown"
        assert not rows.filter(data_coverage="complete").exists()
        assert rows.get(pk=populated.pk).coverage_reason
        assert (
            new
            .get_model("schedule", "Match")
            .objects.filter(season_id=populated.pk)
            .count()
            == RECORDED_FIXTURES
        )
        assert (
            new
            .get_model("competition", "Match")
            .objects.filter(season_id=source_only.pk)
            .count()
            == 1
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
