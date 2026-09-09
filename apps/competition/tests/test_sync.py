"""Verify checkpoints, global pacing and failure recovery without real HTTP."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from io import StringIO
import json
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.application.ports import FetchResult
from apps.competition.models import Club, Match, Pool, SyncLease, SyncResource
from apps.competition.services.importer import Importer, enqueue
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.sync import preview_sync, sync
from apps.competition.services.traffic import TrafficGate
from apps.competition.tests.test_importer import match_payload
from apps.game_tracker.models import MatchData
from apps.schedule.models import (
    Match as AppMatch,
    Season,
)


@pytest.fixture
def season() -> Season:
    """Provide a synthetic active season."""
    return Season.objects.create(
        name="2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )


@pytest.mark.django_db
def test_resume_etag_and_not_modified(season: Season) -> None:
    """Successful resources are skipped until due and retain conditional headers."""
    client = Mock(spec=SportlinkClient)
    client.fetch.return_value = FetchResult(200, {"Club": []}, '"v1"')
    with patch("apps.competition.services.traffic.time.sleep"):
        first = sync(season, client, budget=1)
        second = sync(season, client, budget=1)
    assert first["updated"] == 1
    assert second["requests"] == 0
    resource = SyncResource.objects.get()
    assert resource.etag == '"v1"'
    resource.next_sync_at = timezone.now() - timedelta(seconds=1)
    resource.save()
    client.fetch.return_value = FetchResult(304)
    with patch("apps.competition.services.traffic.time.sleep"):
        third = sync(season, client, budget=1)
    assert third["unchanged"] == 1
    resource.refresh_from_db()
    assert resource.etag == '"v1"'


@pytest.mark.django_db
@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_and_rate_limit_stop_entire_run(season: Season, status: int) -> None:
    """Do not hammer other queued resources when a session/rate limit fails."""
    client = Mock(spec=SportlinkClient)
    client.fetch.return_value = FetchResult(status, retry_after=120)
    result = sync(season, client, budget=10)
    assert result["requests"] == 1
    assert result["failed"] == 1
    assert SyncLease.objects.get().expires_at > timezone.now()
    assert SyncResource.objects.get().last_error == f"http_{status}"


@pytest.mark.django_db
def test_malformed_payload_does_not_checkpoint(season: Season) -> None:
    """Retry schema failures without marking a partially imported directory fresh."""
    client = Mock(spec=SportlinkClient)
    client.fetch.return_value = FetchResult(
        200, {"Club": [{"ClubId": "1", "ClubName": "Test"}, {}]}
    )
    with patch("apps.competition.services.traffic.time.sleep"):
        result = sync(season, client, budget=1)
    assert result["failed"] == 1
    assert SyncResource.objects.get().fetched_at is None
    assert not Club.objects.exists()


@pytest.mark.django_db
def test_concurrent_import_is_rejected(season: Season) -> None:
    """Only one provider-wide importer can run at a time."""
    SyncLease.objects.create(
        key="sportlink", expires_at=timezone.now() + timedelta(minutes=5)
    )
    with pytest.raises(ValueError, match="Another import"):
        sync(season, Mock(spec=SportlinkClient))


@pytest.mark.django_db
def test_transport_uses_only_verified_get_and_etag(season: Season) -> None:
    """No redirects or mutation endpoints can receive the stored bearer token."""
    enqueue(season, "clubs")
    resource = SyncResource.objects.get()
    resource.etag = '"known"'
    client = SportlinkClient("synthetic-test-token", user_agent="synthetic-client")
    response = Mock(status_code=304, headers={})
    with patch.object(client.session, "get", return_value=response) as get:
        assert client.fetch(resource).status == response.status_code
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["headers"] == {
        "If-None-Match": '"known"',
        "X-Navajo-Version": "1",
    }
    assert client.session.headers["X-Navajo-Instance"] == "KNKV"
    assert get.call_args.args[0].endswith("/club/Clubs")
    client.close()


@pytest.mark.django_db
def test_request_preview_requires_no_credentials_or_writes(season: Season) -> None:
    """An empty catalogue previews its bootstrap GET without creating a queue."""
    output = StringIO()
    with (
        patch(
            "apps.competition.management.commands.sync_competition.Command._client"
        ) as client,
        patch(
            "apps.competition.management.commands.sync_competition.timezone.localdate",
            return_value=date(2026, 9, 9),
        ),
    ):
        call_command(
            "sync_competition", season=season.name, dry_run=True, stdout=output
        )
    summary = json.loads(output.getvalue())
    assert summary["by_kind"] == {"clubs": 1}
    assert summary["candidate_feed_requests"] == 1
    client.assert_not_called()
    assert not SyncResource.objects.exists()
    assert not SyncLease.objects.exists()


@pytest.mark.django_db
def test_request_preview_counts_shared_feeds_and_budget(season: Season) -> None:
    """Many due matches share a single poule request plus the daily program."""
    now = timezone.now()
    rows = [dict(match_payload(), PublicMatchId=f"M{index}") for index in range(12)]
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": rows}
    )
    enqueue(season, "clubs")
    Match.objects.update(starts_at=now - timedelta(hours=2))
    Pool.objects.update(results_filtered=False)
    SyncResource.objects.update(
        fetched_at=now - timedelta(days=1), next_sync_at=now + timedelta(days=1)
    )
    SyncResource.objects.filter(kind="club_program", source_id="CT1").update(
        next_sync_at=now - timedelta(seconds=1)
    )
    before = list(SyncResource.objects.values())
    summary = preview_sync(season, budget=1)
    assert summary["by_kind"] == {
        "pool_results": 1,
        "club_program": 1,
        "club_results": 2,
    }
    assert summary["candidate_feed_requests"] == sum(summary["by_kind"].values())
    assert summary["batch_feed_requests_upper_bound"] == 1
    assert list(SyncResource.objects.values()) == before
    assert not SyncLease.objects.exists()


@pytest.mark.django_db
def test_sync_publishes_reschedule_then_final_score(
    season: Season, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two shared feed GETs move the native match, then finish its tracker score."""
    now = datetime(2026, 9, 9, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)
    row = match_payload()
    row.update(Status="POSTPONED", HomeResult=None, AwayResult=None)
    Importer(season, now - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    publish_catalogue()
    native = AppMatch.objects.get()
    native_id = native.pk
    now += timedelta(seconds=1)
    enqueue(season, "clubs")
    Pool.objects.update(results_filtered=False)
    SyncResource.objects.update(fetched_at=now, next_sync_at=now + timedelta(days=7))
    SyncResource.objects.filter(kind="club_program", source_id="CT1").update(
        next_sync_at=now - timedelta(seconds=1)
    )
    kickoff = now + timedelta(days=2)
    row.update(Status="SCHEDULED", MatchDateTime=kickoff.isoformat())
    client = Mock(spec=SportlinkClient)

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        if resource.kind == "club_program":
            return FetchResult(200, {"ProgramItemMatchClub": [{"Match": row}]})
        assert resource.kind == "pool_results"
        return FetchResult(
            200,
            {
                "MatchResult": [row],
                "PoolStanding": None,
                "ResultsFiltered": False,
            },
        )

    client.fetch.side_effect = fetch
    with patch("apps.competition.services.traffic.time.sleep"):
        moved = sync(season, client, budget=1)
    assert moved["http_requests"] == 1
    native.refresh_from_db()
    assert native.start_time == kickoff
    row.update(Status="FINAL", HomeResult={"Score": 0}, AwayResult={"Score": 12})
    # Make only the result poll due, after the rescheduled kickoff.
    SyncResource.objects.filter(kind="club_program").update(
        next_sync_at=kickoff + timedelta(days=1)
    )
    with (
        patch(
            "apps.competition.services.sync.timezone.now",
            return_value=kickoff + timedelta(hours=2),
        ),
        patch("apps.competition.services.traffic.time.sleep"),
    ):
        finished = sync(season, client, budget=1)
    assert finished["http_requests"] == 1
    assert finished["failed"] == 0
    assert finished["new_final_results"] == 1
    assert finished["results_observed"] == 1
    assert finished["matches_checked"] == 1
    assert finished["result_delay_seconds_total"] == int(
        timedelta(minutes=45).total_seconds()
    )
    assert finished["http_requests_pool_results"] == 1
    assert AppMatch.objects.get().pk == native_id
    tracker = MatchData.objects.get(match_link=native)
    assert tracker.status == "finished"
    assert tracker.score_source == "knkv"
    assert (tracker.home_score, tracker.away_score) == (0, 12)
