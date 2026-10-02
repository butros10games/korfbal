"""Order historical lineup requests by how likely KNKV is to serve them.

KNKV serves match lineups for whole competition classes or not at all (for
example seniors and A-juniors, but not younger youth). Each season and class
forms a cohort: its first lineups are a sample at the front of the queue, the
rest wait until the sample shows that the class is served. A class whose whole
sample comes back unavailable is skipped without further requests.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.competition.models import HistoricalResource, Match


SAMPLE_SIZE = 20
HELD = "awaiting_class_sample"
UNSERVED = "class_unserved"
# Samples sort before released lineups; held lineups are never ready.
SAMPLE_AT = datetime(2000, 1, 1, tzinfo=UTC)
HOLD = timedelta(days=3650)


def cohort_key(season_id: object, class_name: str | None) -> str:
    """Name the cohort of a season's competition class."""
    return f"{season_id}:{class_name or '-'}"[:120]


def lineups() -> QuerySet[HistoricalResource]:
    """Return all historical lineup checkpoints."""
    return HistoricalResource.objects.filter(provider="app", kind="lineup")


def assign_cohorts(pending: Iterable[HistoricalResource]) -> set[str]:
    """Store each lineup's cohort from its imported match.

    Returns:
        The cohorts of the given lineups.

    """
    rows = list(pending)
    classes = {
        (season_id, external_id): class_name
        for season_id, external_id, class_name in Match.objects.filter(
            season_id__in={row.season_id for row in rows},
            external_id__in={row.source_id for row in rows},
        ).values_list("season_id", "external_id", "pool__class_name")
    }
    for row in rows:
        row.cohort = cohort_key(
            row.season_id, classes.get((row.season_id, row.source_id))
        )
    HistoricalResource.objects.bulk_update(rows, ("cohort",), batch_size=1000)
    return {row.cohort for row in rows}


@transaction.atomic
def plan_cohort(cohort: str) -> str:
    """Release, sample or skip one cohort's pending lineups.

    Returns:
        ``released``, ``sampling`` or ``unserved``.

    """
    members = lineups().filter(cohort=cohort)
    pending = members.filter(state="pending")
    fetched = members.filter(state="fetched").exists()
    tried = members.exclude(state="pending").exclude(reason=UNSERVED).count()
    if fetched:
        pending.filter(reason=HELD).update(reason="", next_attempt_at=timezone.now())
        return "released"
    if tried >= SAMPLE_SIZE:
        pending.update(
            state="blocked", coverage="inaccessible", reason=UNSERVED, etag=""
        )
        return "unserved"
    sample = list(
        pending.order_by("pk").values_list("pk", flat=True)[: SAMPLE_SIZE - tried]
    )
    pending.filter(pk__in=sample).update(reason="", next_attempt_at=SAMPLE_AT)
    pending.exclude(pk__in=sample).update(
        reason=HELD, next_attempt_at=timezone.now() + HOLD
    )
    return "sampling"


def plan_lineups(*, chunk: int = 5000) -> dict[str, int]:
    """Assign cohorts to pending lineups and plan every cohort once.

    Returns:
        How many cohorts were released, are sampling, or were skipped.

    """
    cohorts: set[str] = set()
    while True:
        batch = list(lineups().filter(state="pending", cohort="")[:chunk])
        if not batch:
            break
        cohorts |= assign_cohorts(batch)
    cohorts |= set(
        lineups().filter(state="pending").values_list("cohort", flat=True).distinct()
    )
    counts = {"released": 0, "sampling": 0, "unserved": 0}
    for cohort in sorted(cohorts):
        counts[plan_cohort(cohort)] += 1
    return counts


def settle(resource: HistoricalResource) -> None:
    """Replan a lineup's cohort after its request succeeded or was unavailable."""
    if resource.kind != "lineup" or not resource.cohort:
        return
    state = (
        HistoricalResource.objects
        .filter(pk=resource.pk)
        .values_list("state", flat=True)
        .first()
    )
    if state in {"fetched", "blocked"}:
        plan_cohort(resource.cohort)
