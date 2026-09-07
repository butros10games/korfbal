"""Regression checks for economical, resumable historical batches."""

from datetime import date, timedelta
from unittest.mock import Mock
import uuid

from django.utils import timezone
import pytest

from apps.competition.application.ports import (
    AuthenticationRequiredError,
    FetchResult,
    ProviderCooldownError,
    TransportError,
)
from apps.competition.models import (
    HistoricalResource,
    Match,
    Pool,
    SyncLease,
    SyncResource,
)
from apps.competition.services.history import HistoryUnavailableError, seed
from apps.competition.services.history_checkpoint import checkpoint
from apps.competition.services.history_worker import (
    HistoryBatch,
    fetch_resource,
    local_work,
    next_resource,
    run_history,
)
from apps.competition.services.importer import Importer
from apps.competition.services.traffic import LeaseLostError, TrafficGate
from apps.competition.tests.test_history import (
    FakeClient,
    dataservice_row,
    old_match,
    old_pool,
)
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


pytestmark = pytest.mark.django_db


@pytest.fixture
def history_season() -> Season:
    """Provide a closed calendar year for exact interval assertions."""
    return Season.objects.create(
        name="review-history", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep request reservations durable while bypassing real provider pacing."""
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)


def test_split_bulk_windows_precede_season_wide_match_seeds(
    history_season: Season,
) -> None:
    """Older halves must fill the graph before redundant per-match enrichment."""
    seed(history_season, "dataservice", "match", "1")
    bulk = seed(
        history_season,
        "dataservice",
        "pool_window",
        "10",
        start=date(2025, 1, 1),
        end=date(2025, 6, 30),
    )
    selected = next_resource()
    assert selected is not None
    assert selected.pk == bulk.pk


@pytest.mark.parametrize("missing_score", [False, True])
def test_bulk_reuse_preserves_missing_result_enrichment(
    history_season: Season, missing_score: bool
) -> None:
    """Complete bulk results save one request; incomplete ones still fetch detail."""
    detail = seed(history_season, "app", "match", "M1")
    seed(history_season, "app", "pool", "10")
    payload = old_pool()
    if missing_score:
        payload["MatchResult"][0]["HomeResult"]["Score"] = None
    client = FakeClient([FetchResult(200, payload), FetchResult(200, old_match())])
    summary = run_history(lambda: client, budget=3, publish=False)
    assert summary["http_requests"] == 1 + missing_score
    assert Match.objects.get().home_score == old_match()["HomeResult"]["Score"]
    detail.refresh_from_db()
    assert detail.state == "fetched"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (FetchResult(401), "reauth_required"),
        (FetchResult(403), "access_denied"),
        (
            HistoryUnavailableError("dataservice_credentials_required"),
            "dataservice_credentials_required",
        ),
    ],
)
def test_rejected_access_requires_explicit_retry(
    history_season: Season, response: object, reason: str
) -> None:
    """Rejected resources stop the batch and remain blocked until an explicit retry."""
    resource = seed(history_season, "app", "match", "M1")
    seed(history_season, "app", "match", "M2")
    summary = run_history(lambda: FakeClient([response]), publish=False)
    assert summary["blocked"] == 1
    resource.refresh_from_db()
    assert resource.state == "blocked"
    assert resource.attempts == 0
    assert resource.reason == reason
    assert list(
        HistoricalResource.objects.filter(state="pending").values_list(
            "source_id", flat=True
        )
    ) == ["M2"]


def test_lifecycle_results_take_priority_before_feed_discovery_deadline() -> None:
    """A match needing its result must preempt history despite a future feed TTL."""
    now = timezone.now()
    today = timezone.localdate(now)
    live = Season.objects.create(
        name="review-live",
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=1),
    )
    row = match_payload()
    row.update(
        Status="SCHEDULED",
        HomeResult=None,
        AwayResult=None,
        MatchDateTime=(now - timedelta(hours=2)).isoformat(),
    )
    Importer(live, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    Pool.objects.update(results_filtered=False)
    SyncResource.objects.update(
        fetched_at=now - timedelta(days=1), next_sync_at=now + timedelta(days=1)
    )
    factory = Mock()
    assert run_history(factory)["reason"] == "current_work_due"
    factory.assert_not_called()


def test_connection_cleanup_failure_still_releases_lease() -> None:
    """A broken session close cannot leave all provider imports locked out."""
    client = Mock()
    client.close.side_effect = RuntimeError("synthetic close failure")
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        run_history(lambda: client, publish=False)
    assert SyncLease.objects.get().owner is None


def test_rejected_refresh_is_not_retried_by_resuming_batch(
    history_season: Season,
) -> None:
    """An invalid OAuth refresh cannot consume another wire request on every run."""
    resource = seed(history_season, "app", "match", "M1")
    failed = run_history(
        lambda: FakeClient([AuthenticationRequiredError()]), publish=False
    )
    assert failed["reason"] == "reauth_required"
    assert failed["blocked"] == 1
    resource.refresh_from_db()
    assert resource.state == "blocked"
    assert resource.reason == "reauth_required"
    assert run_history(lambda: FakeClient([]), publish=False)["http_requests"] == 0


@pytest.mark.parametrize("operation", ["local", "not_modified", "checkpoint"])
def test_stale_workers_cannot_mutate_checkpoints(
    history_season: Season, operation: str
) -> None:
    """Both network-free and HTTP 304 paths respect the successor's lease."""
    stale_owner, successor = uuid.uuid4(), uuid.uuid4()
    SyncLease.objects.create(
        key="sportlink",
        owner=successor,
        expires_at=timezone.now() + timedelta(minutes=2),
    )
    resource = seed(history_season, "dataservice", "window", "C1")

    def mutate() -> None:
        if operation == "local":
            local_work(resource, owner=stale_owner)
        elif operation == "not_modified":
            resource.fetched_at = timezone.now()
            fetch_resource(
                resource,
                Mock(fetch=Mock(return_value=FetchResult(304))),
                TrafficGate(1, stale_owner),
            )
        else:
            checkpoint(resource, {}, owner=stale_owner)

    with pytest.raises(SyncLease.DoesNotExist):
        mutate()
    resource.refresh_from_db()
    assert resource.state == "pending"
    assert HistoricalResource.objects.count() == 1
    assert SyncLease.objects.get().owner == successor


def test_lost_lease_during_spacing_stops_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old worker waking after handover cannot spend its reserved request."""
    owner, successor = uuid.uuid4(), uuid.uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=timezone.now() + timedelta(minutes=2)
    )
    monkeypatch.setattr(
        "apps.competition.services.traffic.time.sleep",
        lambda _: SyncLease.objects.update(owner=successor),
    )
    with pytest.raises(LeaseLostError):
        TrafficGate(1, owner).before_request()
    assert SyncLease.objects.get().owner == successor


def test_successful_checkpoint_clears_old_failure_reason(
    history_season: Season,
) -> None:
    """A successful retry cannot keep reporting an obsolete transport failure."""
    resource = seed(history_season, "app", "match", "M1")
    resource.reason, resource.attempts = "invalid_response_or_transport", 3
    resource.save()
    checkpoint(resource, old_match())
    resource.refresh_from_db()
    assert not resource.reason
    assert resource.attempts == 0


@pytest.mark.parametrize("single_day", [False, True])
def test_truncated_response_cannot_be_retried_with_a_conditional_get(
    history_season: Season, single_day: bool
) -> None:
    """A 304 must not turn split or blocked windows into fetched checkpoints."""
    resource = seed(
        history_season,
        "dataservice",
        "window",
        "C1",
        start=date(2025, 5, 10),
        end=date(2025, 5, 10) if single_day else date(2025, 5, 31),
    )
    resource.etag = '"truncated"'
    checkpoint(
        resource,
        {"rows": [dataservice_row()] * 1000, "wire_start": resource.start_date},
    )
    resource.refresh_from_db()
    assert resource.state == ("blocked" if single_day else "split")
    assert not resource.etag


@pytest.mark.parametrize(
    "error",
    [
        TransportError(),
        HistoryUnavailableError("reauth_required"),
        ProviderCooldownError(60),
    ],
)
def test_stale_failure_cannot_overwrite_successor_checkpoint(
    history_season: Season, error: Exception
) -> None:
    """A late failing response cannot corrupt the successor's successful result."""
    owner, successor = uuid.uuid4(), uuid.uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=timezone.now() + timedelta(minutes=2)
    )
    resource = seed(history_season, "app", "match", "M1")

    def stale_response(*_: object) -> None:
        SyncLease.objects.update(owner=successor)
        HistoricalResource.objects.filter(pk=resource.pk).update(state="fetched")
        raise error

    batch = HistoryBatch(Mock(fetch=stale_response), TrafficGate(1, owner))
    with pytest.raises(SyncLease.DoesNotExist):
        batch.process(resource)
    resource.refresh_from_db()
    assert resource.state == "fetched"
    assert resource.attempts == 0
    assert not resource.reason
