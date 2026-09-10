"""Read-only monitoring distinguishes imports from live samples."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
import pytest
from rest_framework import status

from apps.competition.models import Match, SyncResource, SyncRun
from apps.competition.queries.monitoring import day_bounds, monitoring_dashboard
from apps.competition.services.importer import Importer
from apps.competition.services.monitoring import observe_run
from apps.competition.services.polling import PollPlanner
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 9, 12, 13, tzinfo=UTC)


def test_heartbeat_history_retention_and_secret_allowlist(season: Season) -> None:
    """Keep safe counters and prune expired history."""
    old = SyncRun.objects.create(season=season, started_at=NOW - timedelta(days=31))
    recent = SyncRun.objects.create(season=season, started_at=NOW - timedelta(days=29))
    result = {
        "status": "completed",
        "http_requests": 9,
        "session": "private",
        "failed": "private",
        "backlog": {"due_results": 8, "token": "private"},
    }
    with patch("apps.competition.services.monitoring.timezone.now", return_value=NOW):
        assert observe_run(season, lambda: result) is result
    recorded = SyncRun.objects.latest("started_at")
    assert recorded.status == "completed"
    assert recorded.summary == {"http_requests": 9}
    assert recorded.backlog == {"due_results": 8}
    assert recorded.finished_at == NOW
    assert not SyncRun.objects.filter(pk=old.pk).exists()
    assert SyncRun.objects.filter(pk=recent.pk).exists()


def test_failed_run_remains_visible_without_exception_content(season: Season) -> None:
    """Propagate worker errors without storing their private contents."""
    task = Mock(side_effect=RuntimeError("private-provider-payload"))
    with pytest.raises(RuntimeError, match="private-provider-payload"):
        observe_run(season, task)
    record = SyncRun.objects.get()
    assert record.status == "error"
    assert record.finished_at is not None
    assert record.summary == {}


@pytest.mark.parametrize("state", ["idle", "busy_or_cooldown", "session_unavailable"])
def test_non_fetch_heartbeats_are_visible(season: Season, state: str) -> None:
    """An idle or blocked worker must not look like a missing scheduler."""
    observe_run(season, lambda: {"status": state, "http_requests": 0})
    assert SyncRun.objects.get().status == state


def test_monitor_permissions_filters_and_no_provider_requests(season: Season) -> None:
    """Require all exposed model permissions, including on directly requested URLs."""
    url = reverse("admin:competition_monitor")
    client = Client()
    assert client.get(url).status_code == status.HTTP_302_FOUND
    user = get_user_model().objects.create_user(username="monitor", is_staff=True)
    client.force_login(user)
    assert client.get(url).status_code == status.HTTP_403_FORBIDDEN
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="competition",
            codename__in=[
                "view_match",
                "view_syncresource",
                "view_syncrun",
                "view_synclease",
                "view_trafficstate",
            ],
        )
    )
    with patch("apps.competition.tasks.competition_client") as provider:
        response = client.get(url, {"season": str(season.pk), "date": "2026-09-12"})
    assert response.status_code == status.HTTP_200_OK
    assert b"No polling history for this date" in response.content
    assert b"No qualified measurements yet" in response.content
    provider.assert_not_called()
    assert SyncRun.objects.count() == 0
    assert (
        client.get(url, {"date": "invalid"}).status_code == status.HTTP_400_BAD_REQUEST
    )
    assert (
        client.get(url, {"season": "invalid"}).status_code
        == status.HTTP_400_BAD_REQUEST
    )
    assert (
        client.get(url, {"season": "00000000-0000-0000-0000-000000000000"}).status_code
        == status.HTTP_400_BAD_REQUEST
    )


def test_local_day_coverage_and_unknown_history(season: Season) -> None:
    """Group local dates correctly and keep missing measurements unknown."""
    Importer(season, NOW).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    Match.objects.update(starts_at=datetime(2026, 9, 11, 22, 30, tzinfo=UTC))
    with (
        timezone.override("Europe/Amsterdam"),
        patch(
            "apps.competition.queries.monitoring.timezone.now",
            return_value=NOW,
        ),
    ):
        data = monitoring_dashboard(season, date(2026, 9, 12))
    assert data["totals"]["total"] == 1
    assert data["totals"]["final"] == 1
    assert data["daily"][0]["day"] == date(2026, 9, 12)
    assert data["delay_mean"] is None
    assert data["snapshot"] is None


def test_history_scopes_averages_and_current_failure_alerts(season: Season) -> None:
    """Weight samples and isolate unrelated dates and seasons."""
    other = Season.objects.create(
        name="Other", start_date=season.start_date, end_date=season.end_date
    )
    SyncRun.objects.create(season=other, started_at=NOW, summary={"http_requests": 999})
    SyncRun.objects.create(
        season=season,
        started_at=NOW - timedelta(days=1),
        summary={"http_requests": 999},
    )
    SyncRun.objects.create(
        season=season,
        started_at=NOW - timedelta(minutes=40),
        status="idle",
        finished_at=NOW - timedelta(minutes=40),
    )
    for minutes, samples, total in [(10, 2, 240), (1, 1, 300)]:
        SyncRun.objects.create(
            season=season,
            started_at=NOW - timedelta(minutes=minutes),
            finished_at=NOW,
            status="completed",
            summary={
                "http_requests": 5,
                "measured_final_results": samples,
                "measured_delay_seconds_total": total,
                "measured_delay_seconds_max": 300,
                "unmeasured_final_results": 20,
            },
            backlog={"overdue_pending_matches": 7, "candidate_feed_requests": 4},
        )
    SyncResource.objects.create(
        season=season, kind="club_results", next_sync_at=NOW, failures=6
    )
    with patch("apps.competition.queries.monitoring.timezone.now", return_value=NOW):
        data = monitoring_dashboard(season, date(2026, 9, 12))
    expected_requests, mean_minutes, max_minutes, unmeasured, pending = 10, 3, 5, 40, 7
    assert data["counters"]["http_requests"] == expected_requests
    assert data["delay_mean"] == mean_minutes
    assert data["delay_max"] == max_minutes
    assert data["counters"]["unmeasured_final_results"] == unmeasured
    assert data["timeline"][0]["overdue"] is None
    assert data["timeline"][-1]["overdue"] == pending
    assert data["exhausted"] == 1
    assert any("exhausted" in alert for alert in data["alerts"])


@override_settings(
    SPORTLINK_SYNC_ENABLED=True, SPORTLINK_SYNC_SESSION_FILE="/synthetic/session"
)
def test_stale_heartbeat_and_unfinished_worker_are_visible(season: Season) -> None:
    """A killed worker cannot leave a permanently healthy-looking dashboard."""
    SyncRun.objects.create(season=season, started_at=NOW - timedelta(minutes=15))
    with (
        override_settings(SPORTLINK_SYNC_SEASON=season.name),
        patch(
            "apps.competition.queries.monitoring.timezone.now",
            return_value=NOW,
        ),
    ):
        data = monitoring_dashboard(season, date(2026, 9, 12))
    assert any("No scheduler heartbeat" in alert for alert in data["alerts"])
    assert any("unfinished run" in alert for alert in data["alerts"])


def test_dst_calendar_day_has_correct_bounds() -> None:
    """Calendar-day filtering must not assume every day lasts 24 hours."""
    with timezone.override("Europe/Amsterdam"):
        start, end = day_bounds(date(2026, 10, 25))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)


def test_initial_import_is_not_a_measured_result_delay(season: Season) -> None:
    """Monday's initial import of Saturday finals cannot count as live performance."""
    planner = PollPlanner(season, NOW)
    Importer(season, NOW).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    metrics = planner.result_metrics()
    assert metrics["new_final_results"] == 1
    assert metrics["unmeasured_final_results"] == 1
    assert metrics["measured_final_results"] == 0
    assert metrics["measured_delay_seconds_total"] == 0
