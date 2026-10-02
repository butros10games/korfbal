"""Period resolution beside a running import, on real PostgreSQL row locks."""

from datetime import date
from threading import Event, Thread

from django.db import close_old_connections, connection, transaction
from django.utils import timezone
import pytest

from apps.competition.models import Pool
from apps.competition.services.competition_periods import resolve_pool_periods
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import configure_seasons
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Match, Season


pytestmark = [
    pytest.mark.postgres_parity,
    pytest.mark.service_backed,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
    ),
]


def test_period_resolution_skips_a_poule_the_importer_holds() -> None:
    """Publication never waits on (or deadlocks with) an import's poule lock."""
    scope = Season.objects.create(
        name="Locks 2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    configure_seasons(scope, 2026, split_outdoor=True)
    Importer(scope, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    Pool.objects.update(phase="")
    pool = Pool.objects.get()
    locked, release = Event(), Event()

    def importer_transaction() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                Pool.objects.select_for_update(no_key=True).get(pk=pool.pk)
                locked.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    holder = Thread(target=importer_transaction)
    holder.start()
    try:
        assert locked.wait(timeout=10)
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '2s'")
        with transaction.atomic():
            counts = resolve_pool_periods([pool.pk])
        assert counts["deferred"] == 1
        # The publication mode that runs beside imports skips locked rows.
        result = publish_catalogue(
            schedule_changes=RecordingScheduleChanges(), alongside_import=True
        )
        assert result["counts"]["pools_period_pending"] == 1
        assert not Match.objects.exists()
    finally:
        release.set()
        holder.join(timeout=10)
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    pool.refresh_from_db()
    assert pool.phase == "autumn"
    assert Match.objects.get().season.phase == "autumn"
