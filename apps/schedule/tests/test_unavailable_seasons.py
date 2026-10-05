"""Seasons without source data appear only inside a team's or club's history."""

from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest

from apps.schedule.domain.competition_context import (
    AUTUMN,
    FULL_SEASON,
    INDOOR_PHASE,
    SPRING,
)
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_options_payload, unavailable_seasons
from apps.schedule.tests.season_builders import playing_season


def _gap(phase: str, edition: int) -> Season:
    season = playing_season(phase, edition)
    Season.objects.filter(pk=season.pk).update(data_unavailable=True)
    season.data_unavailable = True
    return season


@pytest.mark.django_db
def test_gap_needs_same_discipline_data_on_both_sides() -> None:
    """Only a history that spans the gap in the same discipline lists it."""
    before = playing_season(INDOOR_PHASE, 2022)
    after = playing_season(INDOOR_PHASE, 2024)
    indoor_gap = _gap(INDOOR_PHASE, 2023)
    _gap(AUTUMN, 2023)

    assert unavailable_seasons([after, before]) == [indoor_gap]
    # A team founded after the gap, or one that stopped before it, never shows it.
    assert unavailable_seasons([after]) == []
    assert unavailable_seasons([before]) == []
    assert unavailable_seasons([]) == []


@pytest.mark.django_db
def test_unresolved_seasons_neither_create_nor_hide_gaps() -> None:
    """Only seasons with a stored discipline count as that discipline's history."""
    unresolved = [
        Season.objects.create(name=name, start_date=start, end_date=end)
        for name, start, end in (
            ("Oud", date(2022, 10, 1), date(2023, 6, 30)),
            ("Overlap", date(2023, 11, 1), date(2024, 3, 31)),
            ("Nieuw", date(2024, 10, 1), date(2025, 6, 30)),
        )
    ]
    gap = _gap(INDOOR_PHASE, 2023)

    assert unavailable_seasons(unresolved) == []
    before = playing_season(INDOOR_PHASE, 2022)
    after = playing_season(INDOOR_PHASE, 2024)
    assert unavailable_seasons([after, *unresolved, before]) == [gap]


@pytest.mark.django_db
def test_gap_overlapped_by_own_data_is_not_listed() -> None:
    """A full-year outdoor season covers the outdoor half it overlaps."""
    before = playing_season(SPRING, 2023)
    whole = playing_season(FULL_SEASON, 2024)
    after = playing_season(SPRING, 2025)
    autumn_gap = _gap(AUTUMN, 2024)

    assert unavailable_seasons([after, whole, before]) == []
    assert unavailable_seasons([after, before]) == [autumn_gap]


@pytest.mark.django_db
def test_gap_with_own_data_stays_an_ordinary_season() -> None:
    """Data in a flagged season keeps it a normal choice."""
    before = playing_season(INDOOR_PHASE, 2022)
    gap = _gap(INDOOR_PHASE, 2023)
    after = playing_season(INDOOR_PHASE, 2024)

    assert unavailable_seasons([after, gap, before]) == []
    options = season_options_payload([after, gap, before])
    assert [option["data_unavailable"] for option in options] == [False] * 3


@pytest.mark.django_db
def test_payload_merges_gaps_in_date_order_and_never_makes_them_current() -> None:
    """Gaps sit between their neighbours and carry the unavailable flag."""
    before = playing_season(INDOOR_PHASE, 2022)
    gap = _gap(INDOOR_PHASE, 2023)
    after = playing_season(INDOOR_PHASE, 2024)

    options = season_options_payload([after, before], [gap])

    assert [
        (option["name"], option["data_unavailable"], option["is_current"])
        for option in options
    ] == [
        (after.name, False, False),
        (gap.name, True, False),
        (before.name, False, False),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("coverage", ["unknown", "partial", "unavailable"])
def test_uncertain_gaps_are_entity_scoped_and_partial_choices_remain_browsable(
    coverage: str,
) -> None:
    """Only same-discipline periods inside the entity's actual history appear."""
    before = playing_season(INDOOR_PHASE, 2022)
    after = playing_season(INDOOR_PHASE, 2024)
    gap = playing_season(INDOOR_PHASE, 2023)
    Season.objects.filter(pk=gap.pk).update(data_coverage=coverage)
    gap.data_coverage = coverage
    # A gap in another discipline is not evidence about this entity's indoor history.
    playing_season(AUTUMN, 2023)
    playing_season(INDOOR_PHASE, 2021)

    assert unavailable_seasons([after, before]) == [gap]
    assert unavailable_seasons([after]) == []
    option = season_options_payload([after, before], [gap])[1]
    assert option["data_coverage"] == coverage
    assert option["data_unavailable"] is (coverage == "unavailable")
    assert not option["is_current"]


@pytest.mark.django_db
def test_complete_coverage_is_not_an_inferred_history_gap() -> None:
    """A reviewed complete period is not offered as an unknown entity gap."""
    before = playing_season(INDOOR_PHASE, 2022)
    after = playing_season(INDOOR_PHASE, 2024)
    gap = playing_season(INDOOR_PHASE, 2023)
    Season.objects.filter(pk=gap.pk).update(
        data_coverage="complete", coverage_reason="Reviewed fixture proof"
    )
    assert unavailable_seasons([after, before]) == []


@pytest.mark.django_db
def test_known_entity_content_overrides_an_unavailable_global_marker() -> None:
    """Older clients can still fetch content while new clients see partial coverage."""
    season = _gap(INDOOR_PHASE, 2023)
    season.data_coverage = "unavailable"
    option = season_options_payload([season])[0]
    assert option["data_coverage"] == "partial"
    assert option["data_unavailable"] is False
    assert option["coverage_reason"]


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_migration_marks_2023_and_autumn_2024() -> None:
    """The backfill flags exactly the seasons no source supplies."""
    before = [("schedule", "0012_season_competition_context")]
    after = [("schedule", "0013_season_data_unavailable")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(before)
        seasons = (
            executor.loader
            .project_state(before)
            .apps.get_model("schedule", "Season")
            .objects
        )
        for name, edition, phase, start, end in (
            ("Na seizoen 2023", 2022, SPRING, date(2023, 1, 1), date(2023, 6, 30)),
            ("Voor seizoen 2023", 2023, AUTUMN, date(2023, 7, 1), date(2023, 12, 31)),
            (
                "Zaal seizoen 2023-2024",
                2023,
                INDOOR_PHASE,
                date(2023, 10, 1),
                date(2024, 6, 30),
            ),
            ("Voor seizoen 2024", 2024, AUTUMN, date(2024, 7, 1), date(2024, 12, 31)),
            (
                "Zaal seizoen 2024-2025",
                2024,
                INDOOR_PHASE,
                date(2024, 10, 10),
                date(2025, 6, 1),
            ),
        ):
            seasons.create(
                name=name, edition=edition, phase=phase, start_date=start, end_date=end
            )

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        rows = (
            executor.loader
            .project_state(after)
            .apps.get_model("schedule", "Season")
            .objects.values_list("name", "data_unavailable")
        )
        assert dict(rows) == {
            "Na seizoen 2023": False,
            "Voor seizoen 2023": True,
            "Zaal seizoen 2023-2024": True,
            "Voor seizoen 2024": True,
            "Zaal seizoen 2024-2025": False,
        }
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
