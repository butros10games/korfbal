"""Period resolution beside a running import, on real PostgreSQL row locks."""

from datetime import date, timedelta
from threading import Event, Thread
import time

from django.db import (
    DatabaseError,
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from django.utils import timezone
import pytest

from apps.competition.models import (
    Match as SourceMatch,
    Pool,
    SyncLease,
)
from apps.competition.services.competition_periods import resolve_pool_periods
from apps.competition.services.history import seed
from apps.competition.services.history_editions import import_rows, prepare_edition
from apps.competition.services.importer import Importer
from apps.competition.services.linkage_review import (
    LinkageSelection,
    apply_manifest,
    preview_linkage,
)
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import configure_seasons
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_history_editions import row
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


def test_linkage_repair_waits_for_a_running_publication_pass() -> None:
    """Repairs serialize behind publication instead of locking rows in reverse."""
    scope = Season.objects.create(
        name="Repair 2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    Importer(scope, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = SourceMatch.objects.get()
    SourceMatch.objects.filter(pk=source.pk).update(
        starts_at=source.starts_at + timedelta(days=1)
    )
    manifest = preview_linkage(LinkageSelection(source_ids=("M1",)))
    assert len(manifest["entries"]) == 1
    SyncLease.objects.get_or_create(
        key="publication", defaults={"expires_at": timezone.now()}
    )
    locked, release = Event(), Event()

    def publication_pass() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                SyncLease.objects.select_for_update().get(key="publication")
                locked.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    holder = Thread(target=publication_pass)
    holder.start()
    try:
        assert locked.wait(timeout=10)
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '1s'")
        with pytest.raises(OperationalError, match="lock timeout"):
            apply_manifest(manifest)
    finally:
        release.set()
        holder.join(timeout=10)


def test_import_locks_the_poule_before_its_fixture() -> None:
    """Publication holding a poule can still update its fixture beside an import."""
    scope = Season.objects.create(
        name="Order 2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    Importer(scope, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    pool = Pool.objects.get()
    source = SourceMatch.objects.get()
    importing = Event()
    errors: list[DatabaseError] = []

    def import_again() -> None:
        close_old_connections()
        try:
            importing.set()
            Importer(scope, timezone.now()).apply(
                "club_results", "CT1", {"MatchResult": [match_payload()]}
            )
        except DatabaseError as error:
            errors.append(error)
        finally:
            connection.close()

    importer = Thread(target=import_again)
    with transaction.atomic():
        # Publication resolves the poule's period first, then updates fixtures.
        Pool.objects.select_for_update(no_key=True).get(pk=pool.pk)
        importer.start()
        assert importing.wait(timeout=10)
        time.sleep(0.5)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
        SourceMatch.objects.select_for_update(no_key=True).get(pk=source.pk)
    importer.join(timeout=10)
    assert not errors


def test_historical_relocation_locks_the_poule_before_its_fixture() -> None:
    """Moving a fixture into the full-year season waits for a held poule first."""
    seasons = prepare_edition(2024)
    resource = seed(seasons.indoor, "app", "edition_pool", "7")
    autumn = row("M1", "2024-09-14T15:00:00+0200")
    import_rows(seasons, resource, [autumn])
    source = SourceMatch.objects.get(external_id="M1")
    assert source.season_id == seasons.autumn.pk
    pool = Pool.objects.get(pk=source.pool_id)
    importing = Event()
    errors: list[DatabaseError] = []

    def import_whole_year() -> None:
        close_old_connections()
        try:
            importing.set()
            import_rows(
                seasons, resource, [autumn, row("M2", "2025-04-12T15:00:00+0200")]
            )
        except DatabaseError as error:
            errors.append(error)
        finally:
            connection.close()

    importer = Thread(target=import_whole_year)
    with transaction.atomic():
        # Publication resolves the poule's period first, then updates fixtures.
        Pool.objects.select_for_update(no_key=True).get(pk=pool.pk)
        importer.start()
        assert importing.wait(timeout=10)
        time.sleep(0.5)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
        SourceMatch.objects.select_for_update(no_key=True).get(pk=source.pk)
    importer.join(timeout=10)
    assert not errors
    source.refresh_from_db()
    assert source.season_id != seasons.autumn.pk
