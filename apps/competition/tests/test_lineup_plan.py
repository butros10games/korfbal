"""Historical lineups are sampled per season and class before the rest is sent."""

from datetime import date

from django.utils import timezone
import pytest

from apps.competition.models import HistoricalResource
from apps.competition.services.importer import Importer
from apps.competition.services.lineup_plan import (
    HELD,
    SAMPLE_AT,
    SAMPLE_SIZE,
    UNSERVED,
    assign_cohorts,
    cohort_key,
    plan_cohort,
    plan_lineups,
    settle,
)
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


COHORT = "season:C-jeugd"
EXTRA = 5


def queue(season: Season, count: int, cohort: str = COHORT) -> list[HistoricalResource]:
    """Queue pending lineups of one cohort."""
    return [
        HistoricalResource.objects.create(
            season=season,
            provider="app",
            kind="lineup",
            source_id=f"M{index}",
            key=f"{cohort}-{index}",
            start_date=season.start_date,
            end_date=season.end_date,
            cohort=cohort,
        )
        for index in range(count)
    ]


def states(cohort: str = COHORT) -> dict[str, int]:
    """Count a cohort's lineups by state and reason."""
    counts: dict[str, int] = {}
    for state, reason in HistoricalResource.objects.filter(cohort=cohort).values_list(
        "state", "reason"
    ):
        label = f"{state}/{reason}" if reason else state
        counts[label] = counts.get(label, 0) + 1
    return counts


@pytest.mark.django_db
def test_a_cohort_sends_its_sample_first_and_holds_the_rest(season: Season) -> None:
    """Only the sample is ready, ahead of every other lineup."""
    queue(season, SAMPLE_SIZE + EXTRA)
    assert plan_cohort(COHORT) == "sampling"
    assert states() == {"pending": SAMPLE_SIZE, f"pending/{HELD}": EXTRA}
    ready = HistoricalResource.objects.filter(cohort=COHORT, reason="")
    assert set(ready.values_list("next_attempt_at", flat=True)) == {SAMPLE_AT}
    held = HistoricalResource.objects.filter(cohort=COHORT, reason=HELD)
    assert all(row.next_attempt_at > timezone.now() for row in held)


@pytest.mark.django_db
def test_one_served_lineup_releases_its_cohort(season: Season) -> None:
    """A class that KNKV serves is requested in full."""
    rows = queue(season, SAMPLE_SIZE + EXTRA)
    plan_cohort(COHORT)
    HistoricalResource.objects.filter(pk=rows[0].pk).update(state="fetched")
    assert plan_cohort(COHORT) == "released"
    assert states() == {"fetched": 1, "pending": SAMPLE_SIZE + EXTRA - 1}
    assert not HistoricalResource.objects.filter(
        cohort=COHORT, state="pending", next_attempt_at__gt=timezone.now()
    ).exists()


@pytest.mark.django_db
def test_an_unserved_sample_skips_the_rest_without_requests(season: Season) -> None:
    """A class whose whole sample is unavailable is never requested again."""
    rows = queue(season, SAMPLE_SIZE + EXTRA)
    plan_cohort(COHORT)
    HistoricalResource.objects.filter(
        pk__in=[row.pk for row in rows[:SAMPLE_SIZE]]
    ).update(state="blocked", reason="lineup_unavailable")
    assert plan_cohort(COHORT) == "unserved"
    assert states() == {
        "blocked/lineup_unavailable": SAMPLE_SIZE,
        f"blocked/{UNSERVED}": EXTRA,
    }


@pytest.mark.django_db
def test_cohorts_come_from_the_imported_match_class(season: Season) -> None:
    """Planning assigns each queued lineup its season and competition class."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    lineup = HistoricalResource.objects.create(
        season=season,
        provider="app",
        kind="lineup",
        source_id="M1",
        key="lineup-M1",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    assert plan_lineups() == {"released": 0, "sampling": 1, "unserved": 0}
    lineup.refresh_from_db()
    assert lineup.cohort == cohort_key(season.pk, "Example class")
    assert assign_cohorts([]) == set()


@pytest.mark.django_db
def test_worker_outcomes_replan_the_cohort(season: Season) -> None:
    """The worker settles each lineup outcome; pending outcomes change nothing."""
    rows = queue(season, SAMPLE_SIZE + EXTRA)
    plan_cohort(COHORT)
    settle(rows[0])
    assert states()[f"pending/{HELD}"] == EXTRA
    HistoricalResource.objects.filter(pk=rows[0].pk).update(state="fetched")
    settle(rows[0])
    assert f"pending/{HELD}" not in states()
