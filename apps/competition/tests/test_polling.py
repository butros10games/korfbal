"""Offline checks for shared feed scheduling and durable HTTP budgets."""

from datetime import timedelta
from unittest.mock import Mock, patch
import uuid

from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.application.ports import RequestBudgetError
from apps.competition.models import Match, Pool, SyncLease, SyncResource, TrafficState
from apps.competition.services.importer import Importer
from apps.competition.services.polling import (
    PollPlanner,
    mark_checked,
    next_result_check,
)
from apps.competition.services.sync import sync
from apps.competition.services.traffic import DAILY_LIMIT, HOURLY_LIMIT, TrafficGate
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


@pytest.mark.django_db
@pytest.mark.parametrize("filtered", [False, True])
def test_shared_feed_covers_both_opponents(season: Season, filtered: bool) -> None:
    """One collection checks the fixture; the other club is not immediately fetched."""
    now = timezone.now()
    row = match_payload()
    row.update(Status="SCHEDULED", HomeResult=None, AwayResult=None)
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    Match.objects.update(starts_at=now - timedelta(hours=2))
    Pool.objects.update(results_filtered=filtered)
    SyncResource.objects.update(
        fetched_at=now - timedelta(days=1), next_sync_at=now + timedelta(days=1)
    )
    planner = PollPlanner(season, now)
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == ("club_results" if filtered else "pool_results")
    assert mark_checked(job, now)
    planner.completed(job, checked=True)
    assert planner.next_job() is None
    match = Match.objects.get()
    assert match.results_checked_at == now
    assert match.result_observed_at == now - timedelta(days=1)


@pytest.mark.django_db
def test_filtered_pool_falls_back_to_club(season: Season) -> None:
    """A newly filtered poule response cannot hide pending matches from fallback."""
    now = timezone.now()
    row = match_payload()
    row.update(Status="SCHEDULED", HomeResult=None, AwayResult=None)
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    Match.objects.update(starts_at=now - timedelta(hours=2))
    Pool.objects.update(results_filtered=False)
    SyncResource.objects.update(
        fetched_at=now - timedelta(days=1), next_sync_at=now + timedelta(days=1)
    )
    planner = PollPlanner(season, now)
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == "pool_results"
    Pool.objects.update(results_filtered=True)
    assert not mark_checked(job, now)
    planner.completed(job, checked=False)
    fallback = planner.next_job()
    assert fallback is not None
    assert fallback.resource.kind == "club_results"


@pytest.mark.parametrize(
    ("age", "final", "interval"),
    [
        (2, False, 0.25),
        (8, False, 1),
        (72, False, 24),
        (72, True, 24),
        (240, True, 168),
        (960, True, 720),
    ],
)
def test_polling_windows(age: int, final: bool, interval: float) -> None:
    """Recent pending scores get priority; settled or missing results back off."""
    now = timezone.now()
    row = {
        "starts_at": now - timedelta(hours=age, minutes=90),
        "status": "FINAL" if final else "SCHEDULED",
        "home_score": 1 if final else None,
        "away_score": 1 if final else None,
        "results_checked_at": now,
        "result_observed_at": None,
    }
    assert next_result_check(row, now) == now + timedelta(hours=interval)
    row["starts_at"] = now + timedelta(days=1)
    row["status"] = "SCHEDULED"
    assert next_result_check(row, now) == now + timedelta(days=1, minutes=90)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "limit"), [("hour_requests", HOURLY_LIMIT), ("day_requests", DAILY_LIMIT)]
)
def test_budget_survives_new_gate(field: str, limit: int) -> None:
    """A fresh command cannot reset the shared hourly or daily request allowance."""
    now = timezone.now()
    owner = uuid.uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=now + timedelta(minutes=2)
    )
    TrafficState.objects.create(
        key="sportlink",
        hour_start=now,
        day_start=now,
        next_request_at=now,
        **{field: limit - 1},
    )
    with patch("apps.competition.services.traffic.time.sleep"):
        TrafficGate(10, owner).before_request()
        with pytest.raises(RequestBudgetError):
            TrafficGate(10, owner).before_request()
    assert getattr(TrafficState.objects.get(), field) == limit


@pytest.mark.django_db
def test_oauth_and_get_share_budget(season: Season) -> None:
    """OAuth consumes the last allowance; the collection waits without a failed feed."""
    store = Mock()
    store.data = {"user_agent": "synthetic"}
    store.needs_refresh.return_value = True
    store.access_token = "synthetic"
    client = SportlinkClient("synthetic", store=store)
    response = Mock(status_code=200, headers={})
    with (
        patch.object(client.session, "post", return_value=response) as post,
        patch.object(client.session, "get") as get,
        patch("apps.competition.services.traffic.time.sleep"),
    ):
        result = sync(season, client, budget=1)
    assert post.call_count == 1
    get.assert_not_called()
    assert result["http_requests"] == 1
    assert result["deferred"] == 1
    assert result["failed"] == 0
    assert SyncResource.objects.get().fetched_at is None
    client.close()
