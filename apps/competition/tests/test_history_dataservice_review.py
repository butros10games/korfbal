"""Protect historical Dataservice precision and bounded reconciliation work."""

from datetime import date
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.competition.models import (
    Club,
    HistoricalDiscovery,
    HistoricalResource,
    Match,
    Pool,
    PoolEntry,
)
from apps.competition.services.history import HistoryUnavailableError, seed
from apps.competition.services.history_checkpoint import checkpoint
from apps.competition.services.history_dataservice import (
    affected_pools,
    reconcile_pool_coverage,
    reconcile_standings,
    stamp,
)
from apps.competition.services.importer import Importer
from apps.competition.tests.test_history import dataservice_row
from apps.schedule.models import Season


@pytest.fixture
def season() -> Season:
    """Use fixed historical dates independent of provider access and today's clock."""
    return Season.objects.create(
        name="dataservice-review",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
    )


def club_window(season: Season) -> HistoricalResource:
    """Seed a club's dated result interval and the authoritative club catalogue."""
    for n in (1, 2):
        Club.objects.create(external_id=f"C{n}", name=f"Club {n}")
    return seed(
        season,
        "dataservice",
        "window",
        "C1",
        sport="KORFBALL-VE-WK",
        start=date(2025, 5, 1),
        end=date(2025, 5, 31),
    )


def complete_pool(season: Season, team_count: int = 2) -> HistoricalResource:
    """Build independent dated results, standings, and membership evidence."""
    for n in range(1, team_count + 1):
        Club.objects.create(external_id=f"C{n}", name=f"Club {n}")
    resource = seed(season, "dataservice", "pool", "10", sport="KORFBALL-VE-WK")
    checkpoint(resource, {})
    rows = []
    for n in range(1, team_count + 1, 2):
        row = {**dataservice_row(), "wedstrijdcode": n}
        for side, number in (("thuis", n), ("uit", n + 1)):
            row[f"{side}teamid"] = number
            row[f"{side}team"] = f"Example {number}"
            row[f"{side}teamclubrelatiecode"] = f"C{number}"
        rows.append(row)
    checkpoint(
        HistoricalResource.objects.get(kind="pool_window"),
        {"rows": rows, "wire_start": season.start_date},
    )
    members = [
        {"teamnaam": f"Example {n}", "clubrelatiecode": f"C{n}"}
        for n in range(1, team_count + 1)
    ]
    checkpoint(HistoricalResource.objects.get(kind="members"), {"rows": members})
    checkpoint(
        HistoricalResource.objects.get(kind="standing"),
        {"rows": [{**row, "gespeeldewedstrijden": 1} for row in members]},
    )
    reconcile_pool_coverage()
    return resource


@pytest.mark.django_db
def test_dataservice_club_limit_splits_at_documented_500_rows(season: Season) -> None:
    """The club endpoint truncates at 500 even when a larger limit was requested."""
    resource = club_window(season)
    checkpoint(
        resource,
        {"rows": [dataservice_row()] * 500, "wire_start": resource.start_date},
    )
    resource.refresh_from_db()
    assert resource.state == "split"
    expected_windows = 2
    assert (
        HistoricalResource.objects.filter(kind="window", state="pending").count()
        == expected_windows
    )
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_wrong_dates_at_row_limit_do_not_spawn_more_requests(season: Season) -> None:
    """A saturated response that ignores dates cannot trigger recursive refetches."""
    resource = club_window(season)
    row = {**dataservice_row(), "wedstrijddatum": "2025-06-01T13:30:00+0200"}
    with pytest.raises(HistoryUnavailableError, match="date_filter_not_honored"):
        checkpoint(resource, {"rows": [row] * 500, "wire_start": resource.start_date})
    assert HistoricalResource.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_result_rows_are_imported_and_discovered_once(season: Season) -> None:
    """Identical provider rows share one write and one discovery operation."""
    resource = club_window(season)
    with patch.object(Importer, "match", autospec=True) as import_match:
        checkpoint(
            resource,
            {"rows": [dataservice_row()] * 10, "wire_start": resource.start_date},
        )
    import_match.assert_called_once()
    assert HistoricalResource.objects.filter(kind="match").count() == 1
    assert resource.evidence["rows"] == 1


@pytest.mark.django_db
def test_conflicting_duplicate_results_roll_back_without_guessing(
    season: Season,
) -> None:
    """Never choose whichever duplicate score happened to appear last."""
    resource = club_window(season)
    with pytest.raises(HistoryUnavailableError, match="conflicting_result_identity"):
        checkpoint(
            resource,
            {
                "rows": [dataservice_row(), {**dataservice_row(), "uitslag": "10-15"}],
                "wire_start": resource.start_date,
            },
        )
    assert not Match.objects.exists()
    assert HistoricalResource.objects.count() == 1


@pytest.mark.django_db
def test_regulation_score_survives_documented_shootout_suffix(season: Season) -> None:
    """The optional shootout result does not turn a played match into UNKNOWN."""
    resource = club_window(season)
    checkpoint(
        resource,
        {
            "rows": [{**dataservice_row(), "uitslag": "12-12 (3-2)"}],
            "wire_start": resource.start_date,
        },
    )
    match = Match.objects.get()
    assert (match.status, match.home_score, match.away_score) == ("FINAL", 12, 12)


def test_dataservice_offset_dates_follow_dutch_calendar() -> None:
    """Equivalent UTC and Dutch timestamps belong to the same historical date."""
    assert stamp("2024-12-31T23:30:00Z").date() == date(2025, 1, 1)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["standing", "members"])
def test_duplicate_team_evidence_cannot_prove_complete_coverage(
    season: Season, kind: str
) -> None:
    """Membership equality must preserve multiplicity, including duplicate IDs."""
    resource = complete_pool(season)
    metadata = HistoricalResource.objects.get(kind=kind)
    key = "standings" if kind == "standing" else "members"
    rows = metadata.evidence[key]
    checkpoint(metadata, {"rows": [*rows, rows[0]]})
    reconcile_pool_coverage({metadata.pk})
    resource.refresh_from_db()
    assert resource.coverage == "partial"
    assert Pool.objects.get().results_filtered


@pytest.mark.django_db
def test_date_gaps_cannot_prove_complete_pool_coverage(season: Season) -> None:
    """Agreeing standings cannot substitute for checking the whole season."""
    resource = complete_pool(season)
    window = HistoricalResource.objects.get(kind="pool_window")
    window.start_date = date(2025, 5, 1)
    window.end_date = date(2025, 5, 31)
    window.save(update_fields=("start_date", "end_date"))
    reconcile_pool_coverage({window.pk})
    resource.refresh_from_db()
    assert resource.coverage == "partial"
    assert not resource.evidence["intervals_exhausted"]


@pytest.mark.django_db
def test_redundant_pending_window_does_not_erase_exhausted_coverage(
    season: Season,
) -> None:
    """An already verified interval union remains valid after overlapping discovery."""
    resource = complete_pool(season)
    overlap = seed(
        season,
        "dataservice",
        "pool_window",
        "10",
        start=date(2025, 5, 1),
        end=date(2025, 5, 31),
    )
    reconcile_pool_coverage({overlap.pk})
    resource.refresh_from_db()
    assert resource.coverage == "complete"


@pytest.mark.django_db
def test_removed_or_unresolved_teams_lose_stale_standings(season: Season) -> None:
    """Older official standings must not remain published when evidence disappears."""
    complete_pool(season)
    metadata = HistoricalResource.objects.get(kind="standing")
    rows = metadata.evidence["standings"]
    checkpoint(metadata, {"rows": rows[:1]})
    reconcile_pool_coverage({metadata.pk})
    assert PoolEntry.objects.get(team__external_id="ds:2").standing == {}


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["standing", "members"])
def test_malformed_metadata_cannot_persist_unusable_coverage_evidence(
    season: Season, kind: str
) -> None:
    """Reject unhashable provider identities inside the response transaction."""
    complete_pool(season)
    resource = HistoricalResource.objects.get(kind=kind)
    original = resource.evidence
    with pytest.raises(ValueError, match="identities must be strings"):
        checkpoint(resource, {"rows": [{"teamnaam": [], "clubrelatiecode": "C1"}]})
    resource.refresh_from_db()
    assert resource.evidence == original


@pytest.mark.django_db
@pytest.mark.parametrize("team_count", [2, 20])
def test_unchanged_standings_cost_two_queries_for_any_team_count(
    season: Season,
    team_count: int,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    """Reconciliation batches identity reads and performs no unchanged row writes."""
    resource = complete_pool(season, team_count)
    pool = Pool.objects.get()
    rows = HistoricalResource.objects.get(kind="standing").evidence["standings"]
    with django_assert_num_queries(2):
        counts = reconcile_standings(resource, pool, rows)
    assert len(counts) == team_count


@pytest.mark.django_db
def test_affected_pool_lookup_batches_match_resources(season: Season) -> None:
    """Many touched details share one joined match lookup without season N+1s."""
    complete_pool(season, 20)
    identifiers = {
        seed(season, "dataservice", "match", str(n)).pk for n in range(1, 21, 2)
    }
    with CaptureQueriesContext(connection) as queries:
        scope = affected_pools(identifiers)
    assert scope == {(season.pk, "10")}
    expected_queries = 2
    assert len(queries) == expected_queries


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["match", "pool_window"])
def test_reassigned_match_invalidates_previous_pool_coverage(
    season: Season, kind: str
) -> None:
    """A corrected pool association must reconcile both sides of the move."""
    previous = complete_pool(season)
    resource = seed(
        season,
        "dataservice",
        kind,
        "1" if kind == "match" else "20",
        sport=previous.sport,
    )
    if kind == "match":
        data = {
            "wedstrijdinformatie": {
                "wedstrijddatetime": "2025-05-10T13:30:00+0200",
                "thuisteamid": 1,
                "uitteamid": 2,
                "poulecode": 20,
            }
        }
    else:
        data = {"rows": [dataservice_row()], "wire_start": season.start_date}
    checkpoint(resource, data)
    assert affected_pools({resource.pk}) == {(season.pk, "10"), (season.pk, "20")}
    reconcile_pool_coverage({resource.pk})
    previous.refresh_from_db()
    assert previous.coverage == "partial"
    assert Pool.objects.get(external_id="ds:10").results_filtered


@pytest.mark.django_db
@pytest.mark.parametrize("revalidate", [False, True])
def test_pool_bulk_reuses_details_but_preserves_explicit_revalidation(
    season: Season, revalidate: bool
) -> None:
    """Verified bulk pool associations share checkpoints without cancelling retries."""
    window = club_window(season)
    checkpoint(window, {"rows": [dataservice_row()], "wire_start": window.start_date})
    detail = HistoricalResource.objects.get(kind="match")
    if revalidate:
        detail.fetched_at = timezone.now()
        detail.save(update_fields=("fetched_at",))
    bulk = seed(season, "dataservice", "pool_window", "10", sport=window.sport)
    checkpoint(bulk, {"rows": [dataservice_row()], "wire_start": season.start_date})
    detail.refresh_from_db()
    assert detail.state == ("pending" if revalidate else "fetched")
    if not revalidate:
        assert detail.evidence == {"reused_match": Match.objects.get().pk}
        assert HistoricalDiscovery.objects.filter(resource=detail, parent=bulk).exists()


@pytest.mark.django_db
def test_wrong_pool_identity_cannot_reassign_results(season: Season) -> None:
    """A provider row naming another pool must not mutate the existing association."""
    complete_pool(season)
    resource = HistoricalResource.objects.get(kind="pool_window")
    with pytest.raises(
        HistoryUnavailableError, match="dataservice_pool_scope_mismatch"
    ):
        checkpoint(
            resource,
            {
                "rows": [{**dataservice_row(), "poulecode": "20"}],
                "wire_start": season.start_date,
            },
        )
    assert Match.objects.get().pool.external_id == "ds:10"
