"""Automatic sync uses synthetic providers and the same durable request limits."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from django.test import override_settings
from django.utils import timezone
from korfbal.celery import app
import pytest

from apps.competition.application.ports import FetchResult
from apps.competition.models import SyncLease, SyncResource
from apps.competition.services.importer import enqueue
from apps.competition.services.traffic import TrafficGate
from apps.competition.tasks import sync_current_competition
from apps.schedule.models import Season


@pytest.fixture
def configured_sync(season: Season) -> Iterator[Mock]:
    """Pin one source season and a fabricated private session path.

    Yields:
        Mock: The synthetic competition client.

    """
    with (
        override_settings(
            SPORTLINK_SYNC_ENABLED=True,
            SPORTLINK_SYNC_SEASON=season.name,
            SPORTLINK_SYNC_SESSION_FILE="/synthetic/session.json",
            SPORTLINK_SYNC_MAX_REQUESTS=1,
        ),
        patch(
            "apps.competition.tasks.timezone.now",
            return_value=datetime(2026, 9, 9, tzinfo=UTC),
        ),
        patch("apps.competition.tasks.competition_client") as factory,
        patch("apps.competition.services.traffic.time.sleep"),
    ):
        yield factory.return_value


def test_disabled_scheduler_does_not_open_client() -> None:
    """Fresh installations and test settings never initiate provider traffic."""
    with patch("apps.competition.tasks.competition_client") as factory:
        assert sync_current_competition() == {"status": "disabled", "http_requests": 0}
    factory.assert_not_called()


@override_settings(SPORTLINK_SYNC_ENABLED=True)
def test_missing_scheduler_configuration_does_not_open_client() -> None:
    """Missing credentials/scope are reported without loading a session or database."""
    with patch("apps.competition.tasks.competition_client") as factory:
        assert sync_current_competition()["status"] == "configuration_required"
    factory.assert_not_called()


@pytest.mark.django_db
def test_scheduled_batch_honors_budget_and_resumes(
    configured_sync: Mock, season: Season
) -> None:
    """The next tick skips completed work and sends only remaining due feed requests."""
    enqueue(season, "club_program", "synthetic-club")

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        return FetchResult(
            200,
            {"ProgramItemMatchClub": []}
            if resource.kind == "club_program"
            else {"Club": []},
        )

    configured_sync.fetch.side_effect = fetch
    first = sync_current_competition()
    assert first["http_requests"] == 1
    assert first["failed"] == 0
    assert first["deferred"] == 1
    configured_sync.close.assert_called_once()
    second = sync_current_competition()
    assert second["http_requests"] == 1
    assert second["failed"] == 0
    idle = sync_current_competition()
    assert idle["http_requests"] == 0
    assert configured_sync.fetch.call_count == len({"club_program", "clubs"})
    assert not SyncResource.objects.filter(fetched_at__isnull=True).exists()


@pytest.mark.django_db
def test_scheduled_batch_skips_provider_lease(configured_sync: Mock) -> None:
    """Concurrent workers and provider cooldowns cannot send an overlapping request."""
    SyncLease.objects.create(
        key="sportlink", expires_at=timezone.now() + timedelta(minutes=10)
    )
    assert sync_current_competition() == {
        "status": "busy_or_cooldown",
        "http_requests": 0,
    }
    configured_sync.fetch.assert_not_called()
    configured_sync.close.assert_not_called()


@pytest.mark.django_db
def test_scheduler_rejects_expired_season(configured_sync: Mock) -> None:
    """A new season needs an explicit source scope rather than mixing live feeds."""
    with patch(
        "apps.competition.tasks.timezone.localdate",
        return_value=datetime(2027, 7, 1, tzinfo=UTC).date(),
    ):
        assert sync_current_competition()["status"] == "inactive_season"
    configured_sync.fetch.assert_not_called()


@pytest.mark.django_db
def test_scheduler_sanitizes_session_errors(
    configured_sync: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed session content cannot appear in task results or logged exceptions."""
    with patch(
        "apps.competition.tasks.competition_client",
        side_effect=ValueError("synthetic-private-value"),
    ):
        assert sync_current_competition()["status"] == "session_unavailable"
    assert "synthetic-private-value" not in caplog.text
    configured_sync.fetch.assert_not_called()


@pytest.mark.django_db
def test_default_scheduler_drains_due_work_without_arbitrary_feed_caps(
    configured_sync: Mock, season: Season
) -> None:
    """Due work can exceed ten requests and two routine feeds within the run window."""
    background_feeds = 12
    for index in range(background_feeds):
        enqueue(season, "club_program", f"synthetic-{index}")

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        return FetchResult(
            200,
            {"ProgramItemMatchClub": []}
            if resource.kind == "club_program"
            else {"Club": []},
        )

    configured_sync.fetch.side_effect = fetch
    with override_settings(SPORTLINK_SYNC_MAX_REQUESTS=0):
        summary = sync_current_competition()
    assert summary["http_requests"] == background_feeds + 1
    assert summary["http_requests_club_program"] == background_feeds
    assert summary["http_requests_clubs"] == 1
    assert summary["backlog"]["candidate_feed_requests"] == 0


@pytest.mark.django_db
def test_scheduler_resumes_after_worker_window_expires(configured_sync: Mock) -> None:
    """An expired work window makes no provider call and leaves discovery queued."""
    with (
        override_settings(SPORTLINK_SYNC_MAX_REQUESTS=0, SPORTLINK_SYNC_MAX_SECONDS=1),
        patch("apps.competition.services.sync.time.monotonic", side_effect=[0, 2, 2]),
    ):
        configured_sync.fetch.side_effect = lambda resource, gate: gate.before_request()
        summary = sync_current_competition()
    assert summary["deferred"] == 1
    assert summary["http_requests"] == 0
    assert summary["backlog"]["candidate_feed_requests"] == 1
    assert SyncResource.objects.get().fetched_at is None


@pytest.mark.django_db
def test_idle_heartbeat_does_not_open_oauth_session(
    configured_sync: Mock, season: Season
) -> None:
    """More frequent local scheduling must not turn idle heartbeats into OAuth calls."""
    SyncResource.objects.create(
        season=season,
        kind="clubs",
        fetched_at=timezone.now(),
        next_sync_at=timezone.now() + timedelta(days=1),
    )
    with patch("apps.competition.tasks.competition_client") as factory:
        assert sync_current_competition()["status"] == "idle"
    factory.assert_not_called()
    configured_sync.fetch.assert_not_called()


def test_heartbeat_expires_before_another_tick() -> None:
    """Keep delayed jobs from replaying a burst of obsolete scheduler ticks."""
    heartbeat = app.conf.beat_schedule["sync-current-competition"]
    interval = 30
    assert heartbeat["schedule"] == interval
    assert heartbeat["options"]["expires"] == interval
