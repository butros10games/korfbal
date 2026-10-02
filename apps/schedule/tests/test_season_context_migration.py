"""The season-context migration records only unambiguous importer evidence."""

from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest


BEFORE = [("schedule", "0011_match_schedule_match_distinct_teams_and_more")]
AFTER = [("schedule", "0012_season_competition_context")]
COMPETITION_BEFORE = [("competition", "0032_incremental_team_elo")]
COMPETITION_AFTER = [("competition", "0033_competition_periods")]


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_backfill_keeps_seasons_and_bindings() -> None:
    """Canonical names with consistent dates get context; nothing is guessed."""
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([*BEFORE, *COMPETITION_BEFORE])
        old = executor.loader.project_state([*BEFORE, *COMPETITION_BEFORE]).apps
        seasons = old.get_model("schedule", "Season").objects
        created = {
            name: seasons.create(name=name, start_date=start, end_date=end).pk
            for name, start, end in (
                ("Voor seizoen 2024", date(2024, 7, 1), date(2024, 12, 31)),
                ("Na seizoen 2025", date(2025, 1, 1), date(2025, 6, 30)),
                ("Zaal seizoen 2024-2025", date(2024, 10, 1), date(2025, 6, 30)),
                ("Veld seizoen 2024-2025", date(2024, 7, 1), date(2025, 6, 30)),
                # Canonical name, contradicting dates: phase stays unresolved.
                ("Na seizoen 2019", date(2018, 7, 1), date(2018, 12, 31)),
                # Free-form name: only the unambiguous edition is recorded.
                ("Competitie 2023", date(2023, 8, 1), date(2024, 5, 31)),
                # Crossing July: nothing can be inferred.
                ("Oude import", date(2019, 3, 1), date(2019, 9, 30)),
            )
        }
        binding = old.get_model("competition", "SeasonBinding").objects.create(
            scope_id=created["Veld seizoen 2024-2025"],
            sport="KORFBALL-ZA-WK",
            season_id=created["Zaal seizoen 2024-2025"],
        )

        executor = MigrationExecutor(connection)
        executor.migrate([*AFTER, *COMPETITION_AFTER])
        new = executor.loader.project_state([*AFTER, *COMPETITION_AFTER]).apps
        rows = {
            row.name: (row.edition, row.discipline, row.phase, row.context_source)
            for row in new.get_model("schedule", "Season").objects.all()
        }
        assert rows == {
            "Voor seizoen 2024": (2024, "outdoor", "autumn", "legacy_name"),
            "Na seizoen 2025": (2024, "outdoor", "spring", "legacy_name"),
            "Zaal seizoen 2024-2025": (2024, "indoor", "indoor", "legacy_name"),
            "Veld seizoen 2024-2025": (2024, "outdoor", "full_season", "legacy_name"),
            "Na seizoen 2019": (2018, "", "", ""),
            "Competitie 2023": (2023, "", "", ""),
            "Oude import": (None, "", "", ""),
        }
        kept = new.get_model("competition", "SeasonBinding").objects.get(pk=binding.pk)
        assert (kept.sport, kept.phase, kept.season_id) == (
            "KORFBALL-ZA-WK",
            "",
            created["Zaal seizoen 2024-2025"],
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
