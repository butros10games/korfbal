"""Offline checks for shared feed scheduling and durable HTTP budgets."""

from datetime import timedelta
from unittest.mock import Mock, patch
import uuid

from django.test import override_settings
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
from apps.competition.services.resources import ENDPOINTS
from apps.competition.services.sync import sync
from apps.competition.services.traffic import TrafficGate
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
    SyncResource.objects.filter(kind="club_program").update(fetched_at=now)
    planner = PollPlanner(season, now)
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == ("club_results" if filtered else "pool_results")
    job.resource.match_ids = list(Match.objects.values_list("pk", flat=True))
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
    SyncResource.objects.filter(kind="club_program").update(fetched_at=now)
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
        (2, False, 3 / 60),
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
        "starts_at": now - timedelta(hours=age, minutes=75),
        "status": "FINAL" if final else "SCHEDULED",
        "home_score": 1 if final else None,
        "away_score": 1 if final else None,
        "results_checked_at": now,
        "result_observed_at": None,
    }
    assert next_result_check(row, now) == now + timedelta(hours=interval)
    row["starts_at"] = now + timedelta(days=1)
    row["status"] = "SCHEDULED"
    assert next_result_check(row, now) == now + timedelta(days=1, minutes=75)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "limit"), [("hour_requests", 120), ("day_requests", 1000)]
)
@override_settings(SPORTLINK_HOURLY_LIMIT=120, SPORTLINK_DAILY_LIMIT=1000)
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


@pytest.mark.django_db
@pytest.mark.parametrize("includes_other_pool", [False, True])
@pytest.mark.parametrize("audit_due", [False, True])
def test_club_response_reuses_actual_results_across_pool_scopes(
    season: Season, includes_other_pool: bool, audit_due: bool
) -> None:
    """A club response suppresses another score request only for rows it returned."""
    now = timezone.now()
    first = match_payload()
    first.update(Status="SCHEDULED", HomeResult=None, AwayResult=None)
    second = {**first, "PublicMatchId": "M2", "Pool": {"PoolId": 20, "PoolName": "B"}}
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [first, second]}
    )
    Match.objects.update(starts_at=now - timedelta(hours=2))
    Pool.objects.update(results_filtered=False)
    Pool.objects.filter(external_id="10").update(results_filtered=True)
    SyncResource.objects.update(
        fetched_at=now - timedelta(days=1), next_sync_at=now + timedelta(days=1)
    )
    if audit_due:
        SyncResource.objects.filter(kind="club_results", source_id="CT1").update(
            next_sync_at=now - timedelta(seconds=1)
        )
        SyncResource.objects.filter(kind="pool_results", source_id="20").update(
            next_sync_at=now
        )
    SyncResource.objects.filter(kind="club_program").update(fetched_at=now)
    planner = PollPlanner(season, now)
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == "club_results"
    payload = [first, second] if includes_other_pool else [first]
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", job.resource.source_id, {"MatchResult": payload}
    )
    assert mark_checked(job, now + timedelta(seconds=1))
    planner.completed(job, checked=True)
    following = planner.next_job()
    if includes_other_pool and not audit_due:
        assert following is None
    else:
        assert following is not None
        assert following.resource.kind == "pool_results"
        assert following.resource.source_id == "20"


@pytest.mark.django_db
def test_daily_pool_metadata_does_not_delay_due_scores(season: Season) -> None:
    """Daily metadata audits remain unchanged while due fixtures can poll sooner."""
    now = timezone.now()
    row = match_payload()
    row.update(Status="SCHEDULED", HomeResult=None, AwayResult=None)
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    Pool.objects.update(results_filtered=False)
    SyncResource.objects.update(
        fetched_at=now - timedelta(days=1),
        next_sync_at=now + timedelta(hours=ENDPOINTS["pool_results"][3]),
    )
    SyncResource.objects.filter(kind="club_program").update(fetched_at=now)
    Match.objects.update(starts_at=now + timedelta(days=1))
    assert PollPlanner(season, now).next_job() is None
    Match.objects.update(starts_at=now - timedelta(hours=2))
    job = PollPlanner(season, now).next_job()
    assert job is not None
    assert job.resource.kind == "pool_results"
    assert timedelta(hours=ENDPOINTS["pool_results"][3]) == timedelta(days=1)


@pytest.mark.django_db
def test_first_result_poll_at_75_minutes_and_three_minute_rechecks(
    season: Season,
) -> None:
    """Fresh daily metadata cannot delay the end-of-match polling window."""
    now = timezone.now()
    row = match_payload()
    row.update(Status="SCHEDULED", HomeResult=None, AwayResult=None)
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    Pool.objects.update(results_filtered=False)
    SyncResource.objects.update(
        fetched_at=now - timedelta(minutes=5), next_sync_at=now + timedelta(days=1)
    )
    Match.objects.update(starts_at=now - timedelta(minutes=74))
    assert PollPlanner(season, now).next_job() is None
    Match.objects.update(starts_at=now - timedelta(minutes=75))
    SyncResource.objects.filter(kind="club_program").update(fetched_at=now)
    planner = PollPlanner(season, now)
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == "pool_results"
    job.resource.match_ids = list(Match.objects.values_list("pk", flat=True))
    assert mark_checked(job, now)
    SyncResource.objects.filter(pk=job.resource.pk).update(fetched_at=now)
    assert (
        PollPlanner(season, now + timedelta(minutes=2, seconds=59)).next_job() is None
    )
    assert PollPlanner(season, now + timedelta(minutes=3)).next_job() is not None


@pytest.mark.django_db
@pytest.mark.parametrize("rate_limited", [False, True])
@override_settings(SPORTLINK_HOURLY_LIMIT=0, SPORTLINK_DAILY_LIMIT=0)
def test_disabled_quotas_have_no_hidden_fallback(rate_limited: bool) -> None:
    """A prior 429 never reinstates arbitrary quotas behind operator settings."""
    now = timezone.now()
    owner = uuid.uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=now + timedelta(minutes=2)
    )
    initial_hour, initial_day = 5000, 100000
    TrafficState.objects.create(
        key="sportlink",
        hour_start=now,
        day_start=now,
        next_request_at=now,
        hour_requests=initial_hour,
        day_requests=initial_day,
        rate_limited=rate_limited,
    )
    with patch("apps.competition.services.traffic.time.sleep"):
        TrafficGate(None, owner).before_request()
    state = TrafficState.objects.get()
    assert state.hour_requests == initial_hour + 1
    assert state.day_requests == initial_day + 1


@pytest.mark.django_db
@pytest.mark.parametrize("spacing_wait", [0, 30])
def test_run_deadline_prevents_another_reservation(spacing_wait: int) -> None:
    """Elapsed work or pacing defers requests without consuming allowance."""
    now = timezone.now()
    owner = uuid.uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=now + timedelta(minutes=2)
    )
    TrafficState.objects.create(
        key="sportlink",
        hour_start=now,
        day_start=now,
        next_request_at=now + timedelta(seconds=spacing_wait),
    )
    with patch("apps.competition.services.traffic.time.monotonic", return_value=100):
        gate = TrafficGate(None, owner, deadline=100 if not spacing_wait else 110)
        with pytest.raises(RequestBudgetError):
            gate.before_request()
    assert gate.requests == 0
    assert TrafficState.objects.get().day_requests == 0


@pytest.mark.django_db
@pytest.mark.parametrize("days_until_match", [1, 3])
def test_upcoming_schedules_refresh_hourly_without_score_checks(
    season: Season, days_until_match: int
) -> None:
    """Only club programs with upcoming matches get hourly schedule refreshes."""
    now = timezone.now()
    row = match_payload()
    row.update(Status="SCHEDULED", HomeResult=None, AwayResult=None)
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    Match.objects.update(starts_at=now + timedelta(days=days_until_match))
    SyncResource.objects.update(
        fetched_at=now - timedelta(hours=1), next_sync_at=now + timedelta(days=1)
    )
    jobs = PollPlanner(season, now).candidate_jobs()
    assert {job.resource.kind for job in jobs} == (
        {"club_program"} if days_until_match == 1 else set()
    )
    assert all(job.schedule_matches and not job.matches for job in jobs)


@pytest.mark.django_db
def test_expired_deadline_after_wait_never_sends_http(season: Season) -> None:
    """A suspended worker keeps its reservation conservative but cannot send late."""
    now = timezone.now()
    owner = uuid.uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=now + timedelta(minutes=2)
    )
    resource = SyncResource.objects.create(
        season=season, kind="clubs", next_sync_at=now
    )
    client = SportlinkClient("synthetic", user_agent="synthetic")
    gate = TrafficGate(None, owner, deadline=10)
    with (
        patch(
            "apps.competition.services.traffic.time.monotonic", side_effect=[0, 0, 11]
        ),
        patch("apps.competition.services.traffic.time.sleep"),
        patch.object(client.session, "get") as get,
        pytest.raises(RequestBudgetError),
    ):
        client.fetch(resource, gate)
    get.assert_not_called()
    assert gate.requests == 1
    client.close()
