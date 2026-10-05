"""Historical lineups are sampled per season and class before the rest is sent."""

from datetime import date, timedelta

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
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("blocked", "reauth_required"),
        ("blocked", "access_denied"),
        ("blocked", "historical_resource_unavailable"),
        ("blocked", "lineup_invalid"),
        ("failed", "invalid_response_or_transport"),
    ],
)
def test_failure_samples_do_not_prove_unserved_class(
    season: Season, state: str, reason: str
) -> None:
    """Auth, transport and invalid responses neither suppress nor expand a sample."""
    rows = queue(season, SAMPLE_SIZE + EXTRA)
    plan_cohort(COHORT)
    HistoricalResource.objects.filter(
        pk__in=[row.pk for row in rows[:SAMPLE_SIZE]]
    ).update(state=state, reason=reason)
    assert plan_cohort(COHORT) == "sampling"
    assert states() == {
        f"{state}/{reason}": SAMPLE_SIZE,
        f"pending/{HELD}": EXTRA,
    }


@pytest.mark.django_db
def test_mixed_semantic_and_failed_samples_do_not_suppress_class(
    season: Season,
) -> None:
    """One malformed sample cannot complete an endpoint-unavailable proof."""
    rows = queue(season, SAMPLE_SIZE + EXTRA)
    plan_cohort(COHORT)
    HistoricalResource.objects.filter(
        pk__in=[row.pk for row in rows[: SAMPLE_SIZE - 1]]
    ).update(state="blocked", reason="lineup_unavailable")
    HistoricalResource.objects.filter(pk=rows[SAMPLE_SIZE - 1].pk).update(
        state="blocked", reason="lineup_invalid"
    )
    assert plan_cohort(COHORT) == "sampling"
    assert not HistoricalResource.objects.filter(reason=UNSERVED).exists()
    assert states()[f"pending/{HELD}"] == EXTRA


@pytest.mark.django_db
def test_replanning_preserves_attempted_sample_backoff(season: Season) -> None:
    """A repeated planning pass cannot pull a failed sample's retry forward."""
    rows = queue(season, SAMPLE_SIZE + EXTRA)
    plan_cohort(COHORT)
    future = timezone.now() + timedelta(hours=3)
    HistoricalResource.objects.filter(pk=rows[0].pk).update(
        attempts=1, reason="invalid_response_or_transport", next_attempt_at=future
    )
    plan_cohort(COHORT)
    rows[0].refresh_from_db()
    assert rows[0].next_attempt_at == future
    assert rows[0].attempts == 1
    assert rows[0].reason == "invalid_response_or_transport"
    assert states()[f"pending/{HELD}"] == EXTRA


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
