"""Regressions for precise historical deduplication and bounded local import work."""

from copy import deepcopy
from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.competition.models import (
    Club,
    HistoricalDiscovery,
    Match,
    PoolEntry,
    ResultRevision,
    SyncLease,
)
from apps.competition.services.history import (
    pool_coverage,
    reference_label,
    seed,
)
from apps.competition.services.history_archive import import_archive
from apps.competition.services.history_checkpoint import checkpoint
from apps.competition.services.history_worker import local_work
from apps.competition.services.importer import Importer
from apps.competition.tests.test_history import old_match, old_pool
from apps.competition.tests.test_importer import team_payload
from apps.schedule.models import Season


@pytest.fixture
def history_season() -> Season:
    """Use a closed synthetic season for historical observations."""
    return Season.objects.create(
        name="integrity-2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["pending", "fetched"])
def test_repeated_identity_widens_scope_without_repeating_completed_work(
    history_season: Season, state: str
) -> None:
    """A later discovery expands the accepted dates while keeping one checkpoint."""
    resource = seed(
        history_season,
        "app",
        "match",
        "M1",
        start=date(2025, 4, 1),
        end=date(2025, 4, 30),
    )
    resource.state = state
    resource.save(update_fields=("state",))
    expanded = seed(history_season, "app", "match", "M1", reference="second")
    assert expanded.pk == resource.pk
    assert expanded.state == state
    assert expanded.start_date == history_season.start_date
    assert expanded.end_date == history_season.end_date
    assert HistoricalDiscovery.objects.count() == len({"operator", "second"})
    if state == "pending":
        checkpoint(expanded, old_match())
        assert Match.objects.count() == 1


@pytest.mark.django_db
def test_widened_identity_retries_only_a_scope_dependent_block(
    history_season: Season,
) -> None:
    """A scope correction cannot remain stuck behind an old interval rejection."""
    resource = seed(
        history_season,
        "app",
        "match",
        "M1",
        start=date(2025, 4, 1),
        end=date(2025, 4, 30),
    )
    resource.state = "blocked"
    resource.coverage = "inaccessible"
    resource.reason = "interval_mismatch"
    resource.etag = '"old-scope"'
    resource.next_attempt_at = timezone.now() + timedelta(days=1)
    resource.save()
    expanded = seed(history_season, "app", "match", "M1")
    assert expanded.state == "pending"
    assert expanded.coverage == "unknown"
    assert not expanded.reason
    assert not expanded.etag
    assert expanded.next_attempt_at <= timezone.now()
    checkpoint(expanded, old_match())
    assert Match.objects.count() == 1


@pytest.mark.django_db
def test_timestamp_offsets_share_the_same_local_season_boundary() -> None:
    """The same kickoff instant belongs to the same Dutch calendar day."""
    season = Season.objects.create(
        name="new-year", start_date=date(2025, 1, 1), end_date=date(2025, 1, 1)
    )
    row = old_match()
    row["MatchDateTime"] = "2024-12-31T23:30:00Z"
    with timezone.override("Europe/Amsterdam"):
        checkpoint(seed(season, "app", "match", "M1"), row)
    assert Match.objects.count() == 1


@pytest.mark.parametrize(
    "reference",
    ["//user:secret@example.org/archive?token=private", "https:///missing-host"],
)
def test_archive_attribution_rejects_malformed_or_credentialed_urls(
    reference: str,
) -> None:
    """Protocol-relative URLs cannot bypass attribution credential sanitization."""
    with pytest.raises(ValueError, match="HTTPS"):
        reference_label(reference)


@pytest.mark.django_db
def test_duplicate_result_rows_do_not_add_database_work(history_season: Season) -> None:
    """Overlapping identical rows are normalized once within each response."""
    row = old_match()
    now = timezone.now()
    Importer(history_season, now, discover=False).apply(
        "club_results", "", {"MatchResult": [row]}
    )
    with CaptureQueriesContext(connection) as single:
        Importer(history_season, now, discover=False).apply(
            "club_results", "", {"MatchResult": [row]}
        )
    with CaptureQueriesContext(connection) as repeated:
        Importer(history_season, now, discover=False).apply(
            "club_results", "", {"MatchResult": [deepcopy(row) for _ in range(100)]}
        )
    assert len(repeated) == len(single)
    assert Match.objects.count() == 1
    assert ResultRevision.objects.count() == 1


@pytest.mark.django_db
def test_conflicting_duplicates_roll_back_without_choosing_an_arbitrary_score(
    history_season: Season,
) -> None:
    """One response must not silently choose its last contradictory fixture row."""
    first = old_match()
    conflicting = deepcopy(first)
    conflicting["HomeResult"]["Score"] = 15
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        Importer(history_season, timezone.now(), discover=False).apply(
            "club_results", "", {"MatchResult": [first, conflicting]}
        )
    assert not Match.objects.exists()
    assert not Club.objects.exists()


@pytest.mark.django_db
def test_duplicate_summary_can_supply_previously_missing_pool(
    history_season: Season,
) -> None:
    """Deduplication retains additional verified metadata from a repeated row."""
    complete = old_match()
    summary = deepcopy(complete)
    summary.pop("Pool")
    Importer(history_season, timezone.now(), discover=False).apply(
        "club_results", "", {"MatchResult": [summary, complete, summary]}
    )
    assert Match.objects.get().pool.external_id == "10"
    assert ResultRevision.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("score", [True, 1.5, "10", -1])
def test_archive_scores_are_not_coerced_into_invented_integers(
    history_season: Season, score: object
) -> None:
    """Malformed normalized archives cannot truncate or coerce their score values."""
    row = old_match()
    row["HomeResult"]["Score"] = score
    with pytest.raises(ValueError, match="nonnegative integers"):
        Importer(history_season, timezone.now(), discover=False).apply(
            "club_results", "", {"MatchResult": [row]}
        )
    assert not Match.objects.exists()


def archive_document(rows: list[dict]) -> dict:
    """Attribute a synthetic archive with its known canonical clubs."""
    for team in (old_match()["HomeTeam"], old_match()["AwayTeam"]):
        Club.objects.get_or_create(
            external_id=team["Club"]["ClubId"],
            defaults={"name": team["Club"]["ClubName"]},
        )
    return {
        "namespace": "review",
        "source": "https://example.org/book",
        "matches": rows,
    }


@pytest.mark.django_db
def test_archive_canonical_club_reads_scale_with_clubs_not_matches(
    history_season: Season,
) -> None:
    """A large archive resolves repeated canonical club references in one query."""
    rows = []
    for index in range(20):
        row = old_match()
        row["PublicMatchId"] = f"M{index}"
        rows.append(row)
    document = archive_document(rows)
    with CaptureQueriesContext(connection) as queries:
        assert import_archive(history_season, document)["imported"] == len(rows)
    club_reads = [
        query
        for query in queries
        if query["sql"].startswith("SELECT")
        and 'FROM "competition_club"' in query["sql"]
    ]
    assert len(club_reads) == len({"batch", "home", "away"})


@pytest.mark.django_db
def test_archive_unknown_club_is_a_controlled_configuration_error(
    history_season: Season,
) -> None:
    """An unmapped archive club produces a useful failure without partial writes."""
    with pytest.raises(ValueError, match="unknown club"):
        import_archive(
            history_season,
            {"namespace": "review", "source": "book", "matches": [old_match()]},
        )
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_conflicting_archive_duplicates_are_not_silently_discarded(
    history_season: Season,
) -> None:
    """Archive ID deduplication preserves uncertainty about contradictory scores."""
    original = old_match()
    conflict = deepcopy(original)
    conflict["HomeResult"]["Score"] = 15
    with pytest.raises(ValueError, match="Conflicting duplicate archive"):
        import_archive(history_season, archive_document([original, conflict]))
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_network_cooldown_does_not_block_offline_archive_import(
    history_season: Season,
) -> None:
    """An unowned provider cooldown should only pause provider traffic."""
    SyncLease.objects.create(
        key="sportlink", owner=None, expires_at=timezone.now() + timedelta(minutes=5)
    )
    assert (
        import_archive(history_season, archive_document([old_match()]))["imported"] == 1
    )


@pytest.mark.django_db
def test_app_coverage_reads_each_fixture_collection_once(
    history_season: Season,
) -> None:
    """Coverage checks do not fetch every match a second time to recheck scores."""
    resource = seed(history_season, "app", "pool", "10")
    data = old_pool()
    checkpoint(resource, data)
    with CaptureQueriesContext(connection) as queries:
        coverage, evidence = pool_coverage(resource, data)
    assert len(queries) == len({"pool", "members", "matches"})
    assert coverage == "complete"
    assert evidence["matches"] == 1


@pytest.mark.django_db
def test_missing_app_result_updates_previously_partial_pool_coverage(
    history_season: Season,
) -> None:
    """Score enrichment completes existing coverage without refetching tables."""
    detail = seed(history_season, "app", "match", "M1")
    pool = seed(history_season, "app", "pool", "10")
    data = old_pool()
    data["MatchResult"][0]["HomeResult"]["Score"] = None
    checkpoint(pool, data)
    assert pool.coverage == "partial"
    checkpoint(detail, old_match())
    pool.refresh_from_db()
    assert pool.coverage == "complete"


@pytest.mark.django_db
def test_moving_an_app_match_invalidates_its_former_pool_coverage(
    history_season: Season,
) -> None:
    """A formerly complete pool must lose completeness when its fixture moves."""
    pool = seed(history_season, "app", "pool", "10")
    checkpoint(pool, old_pool())
    assert pool.coverage == "complete"
    detail = seed(history_season, "app", "match", "M1")
    row = old_match()
    row["Pool"]["PoolId"] = 11
    checkpoint(detail, row)
    pool.refresh_from_db()
    assert pool.coverage == "partial"


@pytest.mark.django_db
def test_enclosing_pool_reassigns_summaries_that_omit_their_pool(
    history_season: Season,
) -> None:
    """The verified enclosing pool corrects a stale association in summary rows."""
    former = seed(history_season, "app", "pool", "10")
    checkpoint(former, old_pool())
    current = seed(history_season, "app", "pool", "11")
    data = old_pool()
    data["MatchResult"][0].pop("Pool")
    checkpoint(current, data)
    former.refresh_from_db()
    assert Match.objects.get().pool.external_id == "11"
    assert former.coverage == "partial"
    assert current.coverage == "complete"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "condition",
    ["complete", "missing_score", "wrong_sport", "outside_interval", "explicit_retry"],
)
def test_bulk_app_results_only_satisfy_unfetched_complete_details_in_scope(
    history_season: Season, condition: str
) -> None:
    """Bulk reuse preserves scope, missing score enrichment, and explicit rechecks."""
    detail = seed(
        history_season,
        "app",
        "match",
        "M1",
        start=date(2025, 4, 1) if condition == "outside_interval" else date(2025, 5, 1),
        end=date(2025, 4, 30) if condition == "outside_interval" else date(2025, 5, 31),
        sport="KORFBALL-ZA-WK" if condition == "wrong_sport" else "KORFBALL-VE-WK",
    )
    if condition == "explicit_retry":
        detail.fetched_at = timezone.now() - timedelta(days=1)
        detail.save(update_fields=("fetched_at",))
    pool = seed(history_season, "app", "pool", "10")
    data = old_pool()
    if condition == "missing_score":
        data["MatchResult"][0]["HomeResult"]["Score"] = None
    checkpoint(pool, data)
    detail.refresh_from_db()
    assert detail.state == ("fetched" if condition == "complete" else "pending")
    if condition == "complete":
        assert detail.evidence == {"reused_match": Match.objects.get().pk}
        assert HistoricalDiscovery.objects.filter(resource=detail, parent=pool).exists()


@pytest.mark.django_db
def test_unknown_pool_member_prevents_complete_coverage(history_season: Season) -> None:
    """Unresolved membership cannot disappear from the completeness proof."""
    pool = seed(history_season, "app", "pool", "10")
    data = old_pool()
    checkpoint(pool, data)
    extra = Importer(history_season, timezone.now(), discover=False).team(
        team_payload("T3")
    )
    PoolEntry.objects.create(pool=Match.objects.get().pool, team=extra)
    assert pool_coverage(pool, data)[0] == "partial"


@pytest.mark.django_db
@pytest.mark.parametrize("filtered", [None, 0, ""])
def test_unknown_filter_evidence_cannot_prove_complete_coverage(
    history_season: Season, filtered: object
) -> None:
    """Only an explicit false provider flag proves that results were not filtered."""
    pool = seed(history_season, "app", "pool", "10")
    data = old_pool()
    checkpoint(pool, data)
    data["ResultsFiltered"] = filtered
    assert pool_coverage(pool, data)[0] == "partial"


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["club", "sport"])
def test_duplicate_team_ids_cannot_hide_different_parent_identities(
    history_season: Season, field: str
) -> None:
    """A repeated match row cannot change a team's club or sport silently."""
    original = old_match()
    conflict = deepcopy(original)
    if field == "club":
        conflict["HomeTeam"]["Club"]["ClubId"] = "different"
    else:
        conflict["HomeTeam"]["SportId"] = "different"
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        Importer(history_season, timezone.now(), discover=False).apply(
            "club_results", "", {"MatchResult": [original, conflict]}
        )
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_explicitly_retried_split_parent_rechecks_completed_children(
    history_season: Season,
) -> None:
    """A changed saturated response cannot reuse permanently cached child evidence."""
    parent = seed(history_season, "dataservice", "window", "C1")
    assert local_work(parent)
    children = list(
        HistoricalDiscovery.objects.filter(parent=parent).select_related("resource")
    )
    for edge in children:
        child = edge.resource
        child.state, child.coverage = "fetched", "complete"
        child.fetched_at = timezone.now()
        child.etag = '"prior"'
        child.save()
    call_command(
        "import_competition_history", "retry", resource=parent.pk, stdout=StringIO()
    )
    parent.refresh_from_db()
    assert local_work(parent)
    for edge in children:
        child = edge.resource
        child.refresh_from_db()
        assert child.state == "pending"
        assert child.coverage == "unknown"
        assert child.fetched_at is not None
        assert not child.etag
