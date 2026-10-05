"""Blank-context publication is opt-in, bounded and records failed attempts."""

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
import pytest
from pytest_django.fixtures import Settings

from apps.competition.models import Pool
from apps.competition.services.competition_periods import PERIOD_VERSION, period_backlog
from apps.competition.services.publishing import Publisher
from apps.schedule.models import Season, SeasonPool


pytestmark = pytest.mark.django_db


def test_publication_backlog_requires_opt_in_and_settles_unresolved_once(
    season: Season,
    settings: Settings,
) -> None:
    """Already-linked pools receive one attempted decision within the configured cap."""
    pools = [
        Pool.objects.create(
            season=season,
            external_id=f"blank-{number}",
            sport="KORFBALL-VE-WK",
            local_pool=SeasonPool.objects.create(
                season=season, name=f"Synthetic {number}", sport="KORFBALL-VE-WK"
            ),
        )
        for number in range(3)
    ]
    settings.SPORTLINK_CONTEXT_BACKLOG_LIMIT = 0
    with transaction.atomic():
        Publisher(None).pools()
    assert list(Pool.objects.values_list("phase_evidence", flat=True)) == [{}, {}, {}]
    settings.SPORTLINK_CONTEXT_BACKLOG_LIMIT = 1
    with transaction.atomic():
        Publisher(None).pools()
    pools[0].refresh_from_db()
    assert not pools[0].phase
    assert pools[0].phase_evidence == {
        "version": PERIOD_VERSION,
        "decision": {"autumn": 0, "spring": 0, "reason": "no_fixtures"},
    }
    assert period_backlog(1) == [pools[1].pk]
    settings.SPORTLINK_CONTEXT_BACKLOG_LIMIT = 2
    with transaction.atomic():
        Publisher(None).pools()
    assert period_backlog() == []
    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        Publisher(None).pools()
    assert not any(query["sql"].lstrip().startswith("UPDATE") for query in queries)


def test_period_backlog_skips_reviewed_or_previously_attempted_blank_pools(
    season: Season,
) -> None:
    """Later rules changes must deliberately replan reasons rather than retry passes."""
    Pool.objects.create(
        season=season,
        external_id="attempted",
        phase_evidence={"decision": {"reason": "ambiguous_halves"}},
    )
    candidate = Pool.objects.create(season=season, external_id="new")
    assert period_backlog(1) == [candidate.pk]
