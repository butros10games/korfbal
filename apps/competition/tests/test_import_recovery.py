"""Recover failed imports without losing diagnostics or claiming absent results."""

from datetime import timedelta
from http import HTTPStatus
from io import StringIO
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
import pytest

from apps.competition.application.ports import FetchResult
from apps.competition.models import Match, Pool, SyncLease, SyncResource, SyncRun
from apps.competition.services.monitoring import (
    MAX_FAILURE_DETAILS,
    observe_run,
    outcome,
    progress,
    reconcile_interrupted_runs,
)
from apps.competition.services.polling import PollJob, next_result_check
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.competition.services.sync import checkpoint, preview_sync, sync
from apps.competition.services.traffic import TrafficGate
from apps.competition.tests.test_feed_coverage import seed
from apps.competition.tests.test_match_details import timing
from apps.schedule.models import Season


pytestmark = pytest.mark.django_db


def test_error_retains_stage_partial_progress_and_safe_details(season: Season) -> None:
    """A later worker exception must retain earlier checkpoints without secrets."""
    client = Mock()
    client.fetch.return_value = FetchResult(200, {"Club": []})
    with (
        patch(
            "apps.competition.services.sync.publish_catalogue",
            side_effect=RuntimeError("private-token"),
        ),
        pytest.raises(RuntimeError, match="private-token"),
    ):
        observe_run(season, lambda: sync(season, client))
    run = SyncRun.objects.get()
    assert run.status == "error"
    assert run.summary["updated"] == 1
    assert run.diagnostics["stage"] == "publishing"
    assert run.diagnostics["exception_type"] == "RuntimeError"
    assert run.diagnostics["code_fingerprint"]
    assert "private-token" not in json.dumps(run.diagnostics)
    assert SyncResource.objects.get().fetched_at is not None


def test_malformed_feed_retains_failure_after_successful_retry(season: Season) -> None:
    """Resource success clears its streak, while historical evidence remains."""
    client = Mock()
    client.fetch.return_value = FetchResult(200, {"Club": [{"private": "secret"}]})

    def run() -> dict[str, object]:
        summary = sync(season, client)
        return {**summary, "status": outcome(summary)}

    observe_run(season, run)
    failed = SyncRun.objects.get()
    resource = SyncResource.objects.get()
    assert failed.status == "retrying"
    failure = failed.diagnostics["failures"][0]
    assert failure == {
        "code": "invalid_response",
        "stage": "checkpoint",
        "resource_id": resource.pk,
        "resource_kind": "clubs",
        "exception_type": "KeyError",
    }
    call_command(
        "retry_competition_resources",
        season=season.name,
        kind="clubs",
        apply=True,
        stdout=StringIO(),
    )
    client.fetch.return_value = FetchResult(200, {"Club": []})
    observe_run(season, run)
    resource.refresh_from_db()
    failed.refresh_from_db()
    assert resource.failures == 0
    assert not resource.last_error
    assert failed.diagnostics["failures"] == [failure]


def test_reconciliation_preserves_live_leases_and_fresh_heartbeats(
    season: Season,
) -> None:
    """A different live owner cannot conceal stale work or close a healthy run."""
    now = timezone.now()
    old = now - timedelta(hours=1)
    owner = uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=now + timedelta(minutes=1)
    )
    stale = SyncRun.objects.create(
        season=season, started_at=old, lease_owner=uuid4(), summary={"updated": 7}
    )
    legacy = SyncRun.objects.create(season=season, started_at=old)
    live = SyncRun.objects.create(
        season=season, started_at=old, heartbeat_at=old, lease_owner=owner
    )
    fresh = SyncRun.objects.create(season=season, started_at=old, heartbeat_at=now)
    expected_interrupted = 2
    assert reconcile_interrupted_runs() == expected_interrupted
    for run in (stale, legacy, live, fresh):
        run.refresh_from_db()
    assert stale.status == legacy.status == "interrupted"
    assert stale.summary == {"updated": 7}
    assert live.status == fresh.status == "running"
    assert reconcile_interrupted_runs() == 0


def test_retry_is_scoped_preview_first_and_respects_cooldown(season: Season) -> None:
    """Requeue selected failures without disturbing budgets or healthy checkpoints."""
    now = timezone.now()
    failed = SyncResource.objects.create(
        season=season,
        kind="match_timing",
        source_id="synthetic",
        next_sync_at=now,
        failures=6,
        last_error="invalid_response",
        etag="old",
        fetched_at=now,
    )
    healthy = SyncResource.objects.create(
        season=season,
        kind="match_timing",
        source_id="healthy",
        next_sync_at=now,
        etag="keep",
    )
    other = SyncResource.objects.create(
        season=season, kind="club_results", next_sync_at=now, failures=6
    )
    output = StringIO()
    options = {
        "season": season.name,
        "kind": "match_timing",
        "error_code": "invalid_response",
    }
    call_command("retry_competition_resources", **options, stdout=output)
    assert json.loads(output.getvalue()) == {"dry_run": True, "matched": 1}
    assert not SyncLease.objects.exists()
    lease = SyncLease.objects.create(
        key="sportlink", expires_at=now + timedelta(minutes=1)
    )
    with pytest.raises(CommandError, match="cooldown"):
        call_command("retry_competition_resources", **options, apply=True)
    failed.refresh_from_db()
    assert failed.failures == MAX_FEED_FAILURES
    lease.expires_at = now
    lease.save()
    call_command(
        "retry_competition_resources", **options, apply=True, stdout=StringIO()
    )
    for resource in (failed, healthy, other):
        resource.refresh_from_db()
    assert failed.failures == 0
    assert failed.fetched_at == now
    assert failed.last_error == "invalid_response"
    assert not failed.etag
    assert healthy.etag == "keep"
    assert other.failures == MAX_FEED_FAILURES


@pytest.mark.parametrize("http_status", [200, 304])
@pytest.mark.parametrize("age_hours", [2, 72])
def test_absent_results_back_off_across_runs_without_claiming_coverage(
    season: Season, http_status: int, age_hours: int
) -> None:
    """Both empty owner responses permit backoff, even across one-request batches."""
    now = timezone.now()
    seed(season, 1)
    Pool.objects.update(results_filtered=True)
    Match.objects.update(
        starts_at=now - timedelta(hours=age_hours),
        result_observed_at=None,
        results_checked_at=None,
    )
    SyncResource.objects.filter(kind="club_results").update(
        fetched_at=now - timedelta(hours=age_hours) + timedelta(minutes=30)
    )
    client = Mock()

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        assert resource.kind == "club_results"
        return FetchResult(
            http_status, {"MatchResult": []} if http_status == HTTPStatus.OK else None
        )

    client.fetch.side_effect = fetch
    with patch("apps.competition.services.traffic.time.sleep"):
        sync(season, client, budget=1)
        assert Match.objects.get().missing_result_attempts == 0
        sync(season, client, budget=1)
        match = Match.objects.get()
        assert match.results_checked_at is None
        assert match.results_attempted_at is not None
        assert match.missing_result_attempts == 1
        assert sync(season, client)["requests"] == 0
    preview = preview_sync(season)
    assert preview["missing_provider_results"] == 1
    assert preview["overdue_pending_matches"] == 1
    row = Match.objects.values().get()
    assert next_result_check(row, now) > now
    assert next_result_check(row, now, include_attempts=False) < now


@pytest.mark.parametrize("fallback", ["success", "failure"])
def test_empty_feed_keeps_fallback_and_failed_fallback_stays_due(
    season: Season, fallback: str
) -> None:
    """Never defer a match because only one owner was successfully queried."""
    rows = seed(season, 1)
    Pool.objects.update(results_filtered=True)
    Match.objects.update(result_observed_at=None, results_checked_at=None)
    client = Mock()
    client.fetch.side_effect = [
        FetchResult(200, {"MatchResult": []}),
        FetchResult(200, {"MatchResult": rows})
        if fallback == "success"
        else FetchResult(500),
    ]
    summary = sync(season, client)
    match = Match.objects.get()
    assert client.fetch.call_count == len({"home", "away"})
    assert match.missing_result_attempts == 0
    if fallback == "success":
        assert match.results_checked_at is not None
        assert summary["failed"] == 0
    else:
        assert match.results_checked_at is None
        assert match.results_attempted_at is None
        assert summary["failed"] == 1


def test_exhausted_backfill_reports_stall_without_provider_calls(
    season: Season,
) -> None:
    """An exhausted component cannot be reported as a completed zero-progress batch."""
    seed(season, 1)
    Match.objects.update(playing_time_observed_at=None)
    SyncResource.objects.filter(kind="match_timing").update(failures=6)
    with patch(
        "apps.competition.management.commands.sync_competition.Command._client"
    ) as client:
        with pytest.raises(CommandError, match="stalled"):
            call_command(
                "update_competition_match_details",
                season=season.name,
                stdout=StringIO(),
            )
        client.return_value.fetch.assert_not_called()
    assert SyncRun.objects.get().status == "exhausted"


def test_cup_timing_import_accepts_untimed_penalties_and_optional_extra_time(
    season: Season,
) -> None:
    """Replay the shape that rejected the 68 production cup timing imports."""
    seed(season, 1)
    match = Match.objects.get()
    Match.objects.update(playing_time_observed_at=None)
    resource = SyncResource.objects.get(kind="match_timing")
    resource.next_sync_at = timezone.now()
    resource.save()
    Match.objects.update(starts_at=timezone.now() + timedelta(days=4))
    body = timing()
    body.update(PublicMatchId=match.external_id, EventTimeResolution="NONE")
    body["MatchPeriod"] += [
        {"Description": "1e verlenging", "PlayTime": 5},
        {"Description": "2e verlenging", "PlayTime": 5},
        {"Description": "Strafworpserie", "PlayTime": 0},
    ]
    client = Mock()
    client.fetch.return_value = FetchResult(200, body)
    summary = sync(season, client, budget=1)
    match.refresh_from_db()
    assert summary["failed"] == 0
    assert match.playing_time_minutes == body["Duration"]
    assert match.match_periods == body["MatchPeriod"]


def test_later_result_clears_missing_streak_without_rewriting_content_timestamp(
    season: Season,
) -> None:
    """A confirmed observation ends reconciliation backoff, including on 304."""
    seed(season, 1)
    match = Match.objects.get()
    updated_at = match.updated_at
    Match.objects.update(missing_result_attempts=3, results_attempted_at=timezone.now())
    resource = SyncResource.objects.get(kind="club_results", source_id="H0")
    resource.match_ids = [match.pk]
    job = PollJob(resource, {match.pk}, 1)
    checkpoint(resource, FetchResult(304), job)
    match.refresh_from_db()
    assert match.missing_result_attempts == 0
    assert match.results_checked_at == resource.fetched_at
    assert match.updated_at == updated_at


def test_failure_history_is_bounded_and_keeps_latest_codes(season: Season) -> None:
    """A noisy provider cannot grow a run's diagnostics without bound."""

    def run() -> dict[str, object]:
        for number in range(MAX_FAILURE_DETAILS + 2):
            progress(
                "checkpoint", code=f"synthetic_{number}", error=ValueError("private")
            )
        return {"status": "retrying"}

    observe_run(season, run)
    diagnostics = SyncRun.objects.get().diagnostics
    assert len(diagnostics["failures"]) == MAX_FAILURE_DETAILS
    assert diagnostics["failures"][0]["code"] == "synthetic_2"
    assert "private" not in json.dumps(diagnostics)
