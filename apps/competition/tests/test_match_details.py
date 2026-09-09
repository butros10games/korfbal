"""Captured endpoint contracts and resumable metadata backfills with synthetic data."""

from datetime import timedelta
from http import HTTPStatus
from io import StringIO
import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.sportlink import BASE_URL, SportlinkClient
from apps.competition.application.ports import FetchResult
from apps.competition.models import Match, SyncResource
from apps.competition.services.importer import Importer
from apps.competition.services.match_details import (
    DETAIL_FIELDS,
    preview_details,
    queue_missing_details,
)
from apps.competition.services.polling import MetadataPlanner, PollJob, PollPlanner
from apps.competition.services.sync import backfill_spacing, sync_details
from apps.competition.services.traffic import TrafficGate
from apps.competition.tests.test_feed_coverage import seed
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


def timing() -> dict:
    """Use the supplied v8 structure with a synthetic match identity."""
    return {
        "PublicMatchId": "M1",
        "Duration": 60,
        "EventTimeResolution": "MINUTE",
        "MatchPeriod": [
            {"Description": "1e helft", "PlayTime": 30},
            {"Description": "2e helft", "PlayTime": 30},
        ],
    }


def details(kind: str) -> dict:
    """Keep fixture values invented; rules are preserved as opaque provider metadata."""
    return {
        "match_timing": timing(),
        "match_facility": {
            "FacilityName": "Example Hall",
            "Address": "Example Street 1",
            "ZipCode": "0000AA",
            "City": "Example Town",
            "SubFacilityName": "Pitch 2",
            "FieldType": "INDOOR",
        },
        "match_rules": {"SyntheticRule": {"Description": "Extra time", "Value": False}},
    }[kind]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "path", "version"),
    [
        ("match_timing", "match/MatchResultDetails", 8),
        ("match_facility", "match/MatchFacility", 3),
        ("match_rules", "match/MatchInfo", 1),
    ],
)
def test_captured_request_contract(
    season: Season, kind: str, path: str, version: int
) -> None:
    """Use matching v/header versions, KNKV instance and the originating User-Agent."""
    resource = SyncResource.objects.create(
        season=season, kind=kind, source_id="M1", next_sync_at=timezone.now()
    )
    client = SportlinkClient("synthetic", user_agent="synthetic-agent")
    body = details(kind)
    if kind == "match_timing":
        body["HomeTeamPerson"] = [{"Name": "Synthetic private player"}]
    response = Mock(status_code=200, headers={})
    response.json.return_value = body
    with patch.object(client.session, "get", return_value=response) as get:
        result = client.fetch(resource)
    args, kwargs = get.call_args
    assert args == (BASE_URL + path,)
    assert kwargs["params"] == {"PublicMatchId": "M1", "v": str(version)}
    assert kwargs["headers"]["X-Navajo-Version"] == str(version)
    assert client.session.headers["X-Navajo-Instance"] == "KNKV"
    assert client.session.headers["User-Agent"] == "synthetic-agent"
    if kind == "match_timing":
        assert result.data == timing()
    client.close()


@pytest.mark.django_db
def test_new_import_queues_each_missing_component_once(season: Season) -> None:
    """Discover missing enrichment during normal season imports."""
    for _ in range(2):
        Importer(season, timezone.now()).apply(
            "club_results", "CT1", {"MatchResult": [match_payload()]}
        )
    assert set(
        SyncResource.objects.filter(kind__in=DETAIL_FIELDS).values_list(
            "kind", flat=True
        )
    ) == set(DETAIL_FIELDS)
    for kind in DETAIL_FIELDS:
        Importer(season, timezone.now()).apply(kind, "M1", details(kind))
    assert preview_details(season)["remaining_detail_requests"] == 0
    match = Match.objects.get()
    assert match.match_periods == timing()["MatchPeriod"]
    assert match.facility_details == details("match_facility")
    assert match.match_rules == details("match_rules")


@pytest.mark.django_db
def test_backfill_dry_run_and_resume_do_not_repeat_imported_components(
    season: Season,
) -> None:
    """Resume all three components one request at a time."""
    Importer(season, timezone.now(), discover=False).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    assert not SyncResource.objects.exists()
    output = StringIO()
    call_command(
        "update_competition_match_details",
        season=season.name,
        dry_run=True,
        stdout=output,
    )
    assert json.loads(output.getvalue())["remaining_detail_requests"] == len(
        DETAIL_FIELDS
    )
    assert not SyncResource.objects.exists()
    client = Mock()
    calls = []

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        calls.append(resource.kind)
        return FetchResult(200, details(resource.kind))

    client.fetch.side_effect = fetch
    with (
        override_settings(
            SPORTLINK_REQUEST_SPACING=5, SPORTLINK_BACKFILL_REQUEST_SPACING=0
        ),
        patch(
            "apps.competition.management.commands.sync_competition.competition_client",
            return_value=client,
        ) as factory,
        patch("apps.competition.services.traffic.time.sleep"),
    ):
        for remaining in (2, 1, 0):
            output = StringIO()
            call_command(
                "update_competition_match_details",
                season=season.name,
                session_file=Path("/synthetic/session.json"),
                max_requests=1,
                stdout=output,
            )
            result = json.loads(output.getvalue())
            assert result["http_requests"] == 1
            assert result["request_spacing_seconds"] == 0
            assert result["remaining_detail_requests"] == remaining
        normal_spacing = 5
        assert backfill_spacing(PollPlanner(season, timezone.now())) == normal_spacing
        initial_factory_calls = factory.call_count
        call_command(
            "update_competition_match_details", season=season.name, stdout=StringIO()
        )
        assert factory.call_count == initial_factory_calls
    assert len(calls) == len(set(calls)) == len(DETAIL_FIELDS)
    assert client.close.call_count == len(DETAIL_FIELDS)


@pytest.mark.django_db
def test_backfill_skips_timing_already_observed_in_lineup_details(
    season: Season,
) -> None:
    """An existing v8 duration observation leaves only venue and rules to fetch."""
    Importer(season, timezone.now(), discover=False).apply(
        "club_results", "CT1", {"MatchResult": [{**match_payload(), **timing()}]}
    )
    preview = preview_details(season)
    assert preview["missing_by_kind"] == {
        "match_timing": 0,
        "match_facility": 1,
        "match_rules": 1,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["match_facility", "match_rules"])
def test_wrong_identity_and_stale_metadata_cannot_replace_match(
    season: Season, kind: str
) -> None:
    """Venue/rules remain bound to the requested fixture and their observation order."""
    now = timezone.now()
    Importer(season, now, discover=False).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    Importer(season, now).apply(kind, "M1", details(kind))
    original = Match.objects.values().get()
    with pytest.raises(ValueError, match="Unrecognized match metadata"):
        Importer(season, now).apply(
            kind, "M1", {"PublicMatchId": "OTHER", **details(kind)}
        )
    Importer(season, now - timedelta(days=1)).apply(kind, "M1", {"Changed": True})
    assert Match.objects.values().get() == original


@pytest.mark.django_db
def test_bulk_metadata_queue_has_bounded_queries(season: Season) -> None:
    """A feed-sized batch must not perform three checkpoint lookups per match."""
    seed(season, 12)
    Match.objects.update(
        playing_time_observed_at=None, facility_observed_at=None, rules_observed_at=None
    )
    SyncResource.objects.filter(kind__in=DETAIL_FIELDS).delete()
    ids = set(Match.objects.values_list("pk", flat=True))
    with CaptureQueriesContext(connection) as queries:
        queued = queue_missing_details(season, match_ids=ids)
    assert queued == len(ids) * len(DETAIL_FIELDS)
    max_queue_queries = 5
    max_repeat_queries = 2
    assert len(queries) <= max_queue_queries
    with CaptureQueriesContext(connection) as repeated:
        assert queue_missing_details(season, match_ids=ids) == 0
    assert len(repeated) <= max_repeat_queries


@pytest.mark.django_db
@pytest.mark.parametrize("total", [50, 60])
def test_none_event_resolution_requires_consistent_period_minutes(
    season: Season, total: int
) -> None:
    """NONE resolution carries minutes only when period totals agree."""
    now = timezone.now()
    Importer(season, now, discover=False).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    payload = timing()
    payload.update(
        Duration=total,
        EventTimeResolution="NONE",
        MatchPeriod=[
            {"Description": "1e helft", "PlayTime": 25},
            {"Description": "2e helft", "PlayTime": 25},
        ],
    )
    valid_minutes = 50
    if total == valid_minutes:
        Importer(season, now).apply("match_timing", "M1", payload)
        assert Match.objects.get().playing_time_minutes == valid_minutes
    else:
        with pytest.raises(ValueError, match="Missing or unsupported"):
            Importer(season, now).apply("match_timing", "M1", payload)
        assert Match.objects.get().playing_time_observed_at is None


@pytest.mark.django_db
def test_metadata_queue_filters_once_and_selects_without_queries(
    season: Season,
) -> None:
    """Completed, backed-off, exhausted and unknown components never enter the batch."""
    seed(season, 4)
    now = timezone.now()
    Match.objects.update(facility_observed_at=None)
    SyncResource.objects.filter(kind="match_facility").update(next_sync_at=now)
    SyncResource.objects.filter(kind="match_facility", source_id="M1").update(
        failures=1, next_sync_at=now + timedelta(hours=1)
    )
    SyncResource.objects.filter(kind="match_facility", source_id="M2").update(
        failures=10
    )
    Match.objects.filter(external_id="M3").update(facility_observed_at=now)
    SyncResource.objects.create(
        season=season, kind="match_facility", source_id="missing", next_sync_at=now
    )
    with CaptureQueriesContext(connection) as queries:
        planner = MetadataPlanner(season, now)
    assert len(queries) == 1
    with CaptureQueriesContext(connection) as selections:
        job = planner.next_job()
        assert job is not None
        assert (job.resource.kind, job.resource.source_id) == ("match_facility", "M0")
        planner.completed(job, checked=True)
        assert planner.next_job() is None
        assert planner.result_metrics()["results_observed"] == 0
    assert not selections


@pytest.mark.django_db
@pytest.mark.parametrize("status", [200, 429])
def test_details_run_never_builds_match_planner(season: Season, status: int) -> None:
    """The fast path retains shared quota/cooldown accounting and durable resume."""
    seed(season, 3)
    Match.objects.update(facility_observed_at=None)
    SyncResource.objects.filter(kind="match_facility").update(
        next_sync_at=timezone.now()
    )
    client = Mock()

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        return FetchResult(status, details(resource.kind), retry_after=120)

    client.fetch.side_effect = fetch
    with (
        patch("apps.competition.services.sync.PollPlanner", side_effect=AssertionError),
        patch("apps.competition.services.sync.backfill_spacing", return_value=0),
    ):
        summary = sync_details(season, lambda: client, budget=3)
    expected_requests = 3 if status == HTTPStatus.OK else 1
    assert summary["http_requests"] == expected_requests
    assert summary["updated"] == (3 if status == HTTPStatus.OK else 0)
    assert summary["failed"] == (0 if status == HTTPStatus.OK else 1)
    client.close.assert_called_once()


@pytest.mark.django_db
def test_regular_sync_defers_audits_and_rechecks_due_work(season: Season) -> None:
    """Preempt metadata when an upcoming schedule check becomes due."""
    seed(season, 4)
    now = timezone.now()
    Match.objects.update(
        starts_at=now + timedelta(hours=1),
        schedule_checked_at=now - timedelta(minutes=15) + timedelta(seconds=30),
        facility_observed_at=None,
    )
    SyncResource.objects.filter(kind="match_facility").update(next_sync_at=now)
    SyncResource.objects.filter(kind="clubs").update(next_sync_at=now)
    SyncResource.objects.filter(kind="club_program").update(
        fetched_at=now - timedelta(hours=1)
    )
    planner = PollPlanner(season, now)
    with patch("apps.competition.services.polling.timezone.now", return_value=now):
        job = planner.next_job()
        assert job is not None
        assert job.resource.kind == "match_facility"
        # The second selection does not rebuild the result/schedule candidates.
        with patch.object(planner, "candidate_jobs", side_effect=AssertionError):
            job = planner.next_job()
            assert job is not None
            assert job.resource.kind == "match_facility"
    with patch(
        "apps.competition.services.polling.timezone.now",
        return_value=now + timedelta(seconds=31),
    ):
        job = planner.next_job()
    assert job is not None
    assert job.resource.kind == "club_program"


@pytest.mark.django_db
def test_urgent_results_keep_discovery_slot_during_catchup(season: Season) -> None:
    """The fifth selection must not spend urgent-result capacity on a routine audit."""
    seed(season, 3)
    now = timezone.now()
    Match.objects.update(facility_observed_at=None)
    SyncResource.objects.filter(kind="match_facility").update(next_sync_at=now)
    SyncResource.objects.filter(kind="clubs").update(
        next_sync_at=now - timedelta(days=1)
    )
    planner = PollPlanner(season, now)
    planner.attempted.update({-1, -2, -3, -4})
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind in {"pool_results", "club_results"}
    assert job.urgent_matches


@pytest.mark.django_db
def test_regular_sync_skips_newly_observed_metadata_and_restores_audits(
    season: Season,
) -> None:
    """A lineup can satisfy a queued component; draining it restores routine sync."""
    seed(season, 1)
    now = timezone.now()
    Match.objects.update(
        starts_at=now + timedelta(days=3), playing_time_observed_at=None
    )
    SyncResource.objects.filter(kind="match_timing").update(next_sync_at=now)
    SyncResource.objects.filter(kind="clubs").update(next_sync_at=now)
    planner = PollPlanner(season, now)
    Match.objects.update(playing_time_observed_at=now)
    lineup = SyncResource(
        season=season, kind="match_lineup", source_id="M0", next_sync_at=now
    )
    planner.completed(PollJob(lineup, set(), 2), checked=True)
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == "clubs"
    assert not planner.metadata.jobs
