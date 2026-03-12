"""Tests for audit ingestion and timeline APIs."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.audit.models import AuditEvent


TEST_PASSWORD = "pass1234"  # nosec
EXPECTED_EXTENSION_ROWS = 2
EXPECTED_BULK_CREATED = 2
EXPECTED_CURSOR_PAGE_SIZE = 2
EXPECTED_SUMMARY_WINDOW_HOURS = 24
EXPECTED_SUMMARY_TOTAL = 3
EXPECTED_EXTENSION_SOURCE_COUNT = 2
EXPECTED_SYNC_STARTED_COUNT = 2
EXPECTED_NON_STAFF_VISIBLE_TOTAL = 2
EXPECTED_PRODUCERS_COUNT = 2
EXPECTED_EXTENSION_PRODUCER_TOTAL = 2
EXPECTED_EXTENSION_PRODUCER_ERRORS = 1
EXPECTED_TRENDS_WINDOW_HOURS = 6
EXPECTED_TRENDS_PREVIOUS_ERROR_RATE = 50.0
EXPECTED_TRENDS_CURRENT_ERROR_RATE = 25.0
EXPECTED_HEALTH_WINDOW_HOURS = 6


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_audit_ingest_creates_event_without_token(client: Client) -> None:
    """Ingest endpoint should create an event when token auth is disabled."""
    payload = {
        "event_name": "tracker.goal",
        "source_system": "django",
        "severity": "info",
        "message": "Goal scored",
        "metadata": {"part": 2},
    }

    response = client.post(
        "/api/audit/events/ingest/",
        data=payload,
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED
    assert AuditEvent.objects.count() == 1
    event = AuditEvent.objects.get()
    assert event.event_name == "tracker.goal"
    assert event.source_system == "django"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    KORFBAL_AUDIT_INGEST_TOKEN="top-secret",  # nosec
)
def test_audit_ingest_requires_matching_token(client: Client) -> None:
    """Configured ingest token should be enforced."""
    payload = {
        "event_name": "trade.updated",
        "source_system": "cli",
    }

    response_forbidden = client.post(
        "/api/audit/events/ingest/",
        data=payload,
        content_type="application/json",
    )
    assert response_forbidden.status_code == HTTPStatus.FORBIDDEN

    response_ok = client.post(
        "/api/audit/events/ingest/",
        data=payload,
        content_type="application/json",
        headers={"X-Audit-Token": "top-secret"},
    )
    assert response_ok.status_code == HTTPStatus.CREATED


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_timeline_filters_and_ordering(client: Client) -> None:
    """Timeline should support source/search filters and newest-first ordering."""
    now = timezone.now()
    AuditEvent.objects.create(
        event_name="sync.started",
        source_system="extension",
        occurred_at=now - timedelta(minutes=10),
        severity="info",
        message="sync begin",
    )
    AuditEvent.objects.create(
        event_name="sync.completed",
        source_system="extension",
        occurred_at=now,
        severity="info",
        message="sync end",
    )
    AuditEvent.objects.create(
        event_name="trade.created",
        source_system="cli",
        occurred_at=now - timedelta(minutes=5),
        severity="warning",
        message="trade opened",
    )

    user = get_user_model().objects.create_user(
        username="timeline_user",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)

    response = client.get(
        "/api/audit/events/timeline/",
        {
            "source": "extension",
            "search": "sync",
            "limit": 10,
        },
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["count"] == EXPECTED_EXTENSION_ROWS
    assert payload["items"][0]["event_name"] == "sync.completed"
    assert payload["items"][1]["event_name"] == "sync.started"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_audit_bulk_ingest_creates_multiple_events(client: Client) -> None:
    """Bulk ingest should create all provided events in one call."""
    response = client.post(
        "/api/audit/events/ingest/bulk/",
        data={
            "events": [
                {
                    "event_name": "cli.start",
                    "source_system": "rolltrader_console",
                },
                {
                    "event_name": "cli.finish",
                    "source_system": "rolltrader_console",
                    "severity": "warning",
                },
            ]
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED
    payload = response.json()
    assert payload["created"] == EXPECTED_BULK_CREATED
    assert len(payload["ids"]) == EXPECTED_BULK_CREATED
    assert AuditEvent.objects.count() == EXPECTED_BULK_CREATED


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_timeline_cursor_pagination(client: Client) -> None:
    """Timeline should paginate with cursor and preserve descending order."""
    now = timezone.now()
    for index in range(5):
        AuditEvent.objects.create(
            event_name=f"event.{index}",
            source_system="extension",
            occurred_at=now - timedelta(minutes=index),
            severity="info",
            message=f"event {index}",
        )

    user = get_user_model().objects.create_user(
        username="cursor_user",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)

    first_page = client.get(
        "/api/audit/events/timeline/",
        {
            "limit": 2,
        },
    )
    assert first_page.status_code == HTTPStatus.OK

    first_payload = first_page.json()
    assert first_payload["count"] == EXPECTED_CURSOR_PAGE_SIZE
    assert first_payload["has_more"] is True
    assert first_payload["next_cursor"]

    second_page = client.get(
        "/api/audit/events/timeline/",
        {
            "limit": 2,
            "cursor": first_payload["next_cursor"],
        },
    )
    assert second_page.status_code == HTTPStatus.OK

    second_payload = second_page.json()
    first_ids = {row["id_uuid"] for row in first_payload["items"]}
    second_ids = {row["id_uuid"] for row in second_payload["items"]}
    assert first_ids.isdisjoint(second_ids)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_timeline_invalid_cursor_returns_400(client: Client) -> None:
    """Invalid cursor values should return a validation response."""
    user = get_user_model().objects.create_user(
        username="invalid_cursor_user",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)

    response = client.get(
        "/api/audit/events/timeline/",
        {
            "cursor": "not-a-cursor",
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_summary_returns_aggregates_for_staff(client: Client) -> None:
    """Summary endpoint should return grouped counts for staff users."""
    now = timezone.now()
    AuditEvent.objects.create(
        event_name="sync.started",
        source_system="extension",
        occurred_at=now - timedelta(hours=1),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="sync.started",
        source_system="extension",
        occurred_at=now - timedelta(minutes=10),
        severity="warning",
    )
    AuditEvent.objects.create(
        event_name="trade.failed",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(minutes=5),
        severity="error",
    )

    user = get_user_model().objects.create_user(
        username="summary_staff_user",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)

    response = client.get("/api/audit/events/summary/", {"window_hours": 24})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["window_hours"] == EXPECTED_SUMMARY_WINDOW_HOURS
    assert payload["total"] == EXPECTED_SUMMARY_TOTAL
    assert payload["by_severity"]["info"] == 1
    assert payload["by_severity"]["warning"] == 1
    assert payload["by_severity"]["error"] == 1

    source_counts = {row["source_system"]: row["count"] for row in payload["by_source"]}
    assert source_counts["extension"] == EXPECTED_EXTENSION_SOURCE_COUNT
    assert source_counts["rolltrader_console"] == 1

    event_counts = {row["event_name"]: row["count"] for row in payload["top_events"]}
    assert event_counts["sync.started"] == EXPECTED_SYNC_STARTED_COUNT
    assert event_counts["trade.failed"] == 1


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_summary_filters_for_non_staff_user(client: Client) -> None:
    """Non-staff users should only see rows matching timeline visibility rules."""
    now = timezone.now()

    visible_user = get_user_model().objects.create_user(
        username="summary_visible_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )
    hidden_user = get_user_model().objects.create_user(
        username="summary_hidden_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )

    AuditEvent.objects.create(
        event_name="visible.by.actor",
        source_system="django",
        occurred_at=now - timedelta(minutes=30),
        actor_id=str(visible_user.pk),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="visible.by.club",
        source_system="django",
        occurred_at=now - timedelta(minutes=20),
        club_id="",
        severity="warning",
    )
    AuditEvent.objects.create(
        event_name="hidden.row",
        source_system="django",
        occurred_at=now - timedelta(minutes=10),
        actor_id=str(hidden_user.pk),
        club_id="club-abc",
        severity="critical",
    )

    client.force_login(visible_user)
    response = client.get("/api/audit/events/summary/", {"window_hours": 24})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["total"] == EXPECTED_NON_STAFF_VISIBLE_TOTAL
    by_events = {row["event_name"]: row["count"] for row in payload["top_events"]}
    assert "visible.by.actor" in by_events
    assert "visible.by.club" in by_events
    assert "hidden.row" not in by_events


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_producer_stats_returns_grouped_health_metrics(client: Client) -> None:
    """Producer stats endpoint should expose per-source totals/error counters."""
    now = timezone.now()
    AuditEvent.objects.create(
        event_name="a",
        source_system="extension",
        occurred_at=now - timedelta(minutes=20),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="b",
        source_system="extension",
        occurred_at=now - timedelta(minutes=5),
        severity="error",
    )
    AuditEvent.objects.create(
        event_name="c",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(minutes=2),
        severity="warning",
    )

    user = get_user_model().objects.create_user(
        username="producer_stats_staff",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)

    response = client.get("/api/audit/events/producers/", {"window_hours": 24})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["count"] == EXPECTED_PRODUCERS_COUNT

    items_by_source = {row["source_system"]: row for row in payload["items"]}
    extension_row = items_by_source["extension"]
    assert extension_row["total"] == EXPECTED_EXTENSION_PRODUCER_TOTAL
    assert extension_row["errors"] == EXPECTED_EXTENSION_PRODUCER_ERRORS
    assert extension_row["warnings"] == 0
    assert extension_row["last_seen"] is not None


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_producer_stats_applies_non_staff_visibility_filters(client: Client) -> None:
    """Non-staff producer stats should hide events outside timeline visibility scope."""
    now = timezone.now()
    visible_user = get_user_model().objects.create_user(
        username="producer_visible_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )
    hidden_user = get_user_model().objects.create_user(
        username="producer_hidden_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )

    AuditEvent.objects.create(
        event_name="visible",
        source_system="extension",
        occurred_at=now - timedelta(minutes=3),
        actor_id=str(visible_user.pk),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="hidden",
        source_system="secret_source",
        occurred_at=now - timedelta(minutes=2),
        actor_id=str(hidden_user.pk),
        club_id="club-private",
        severity="debug",
    )

    client.force_login(visible_user)
    response = client.get("/api/audit/events/producers/", {"window_hours": 24})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    items_by_source = {row["source_system"]: row for row in payload["items"]}
    assert "extension" in items_by_source
    assert "secret_source" not in items_by_source


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_trends_returns_hourly_points_and_error_rate_delta(client: Client) -> None:
    """Trends endpoint should provide hourly buckets and error-rate delta."""
    now = timezone.now()

    # Current window: 4 events, 1 error => 25%
    AuditEvent.objects.create(
        event_name="current.info.1",
        source_system="extension",
        occurred_at=now - timedelta(hours=1, minutes=5),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="current.info.2",
        source_system="extension",
        occurred_at=now - timedelta(hours=2, minutes=15),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="current.warning",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(hours=3, minutes=25),
        severity="warning",
    )
    AuditEvent.objects.create(
        event_name="current.error",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(hours=4, minutes=10),
        severity="error",
    )

    # Previous window: 2 events, 1 error => 50%
    AuditEvent.objects.create(
        event_name="previous.error",
        source_system="extension",
        occurred_at=now - timedelta(hours=7),
        severity="error",
    )
    AuditEvent.objects.create(
        event_name="previous.info",
        source_system="extension",
        occurred_at=now - timedelta(hours=8),
        severity="info",
    )

    user = get_user_model().objects.create_user(
        username="trends_staff_user",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)

    response = client.get(
        "/api/audit/events/trends/",
        {"window_hours": EXPECTED_TRENDS_WINDOW_HOURS},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["window_hours"] == EXPECTED_TRENDS_WINDOW_HOURS
    assert len(payload["points"]) == EXPECTED_TRENDS_WINDOW_HOURS + 1
    assert payload["error_rate"]["current"] == EXPECTED_TRENDS_CURRENT_ERROR_RATE
    assert payload["error_rate"]["previous"] == EXPECTED_TRENDS_PREVIOUS_ERROR_RATE
    assert payload["error_rate"]["delta"] == (
        EXPECTED_TRENDS_CURRENT_ERROR_RATE - EXPECTED_TRENDS_PREVIOUS_ERROR_RATE
    )


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_trends_non_staff_applies_visibility_filter(client: Client) -> None:
    """Non-staff trends should exclude hidden debug-only producer events."""
    now = timezone.now()

    visible_user = get_user_model().objects.create_user(
        username="trends_visible_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )
    hidden_user = get_user_model().objects.create_user(
        username="trends_hidden_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )

    AuditEvent.objects.create(
        event_name="visible.info",
        source_system="extension",
        occurred_at=now - timedelta(hours=1),
        actor_id=str(visible_user.pk),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="hidden.debug",
        source_system="secret_source",
        occurred_at=now - timedelta(hours=1, minutes=20),
        actor_id=str(hidden_user.pk),
        club_id="club-private",
        severity="debug",
    )

    client.force_login(visible_user)
    response = client.get(
        "/api/audit/events/trends/",
        {"window_hours": EXPECTED_TRENDS_WINDOW_HOURS},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    summed_total = sum(point["total"] for point in payload["points"])
    assert summed_total == 1


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_health_scores_rank_worst_producer_first(client: Client) -> None:
    """Health endpoint should rank producers by descending risk score."""
    now = timezone.now()

    # extension gets worse: higher current error-rate than previous window
    AuditEvent.objects.create(
        event_name="ext.current.error.1",
        source_system="extension",
        occurred_at=now - timedelta(hours=1),
        severity="error",
    )
    AuditEvent.objects.create(
        event_name="ext.current.error.2",
        source_system="extension",
        occurred_at=now - timedelta(hours=2),
        severity="error",
    )
    AuditEvent.objects.create(
        event_name="ext.current.info.1",
        source_system="extension",
        occurred_at=now - timedelta(hours=2, minutes=10),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="ext.current.warning.1",
        source_system="extension",
        occurred_at=now - timedelta(hours=3),
        severity="warning",
    )

    AuditEvent.objects.create(
        event_name="ext.previous.info.1",
        source_system="extension",
        occurred_at=now - timedelta(hours=7),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="ext.previous.info.2",
        source_system="extension",
        occurred_at=now - timedelta(hours=8),
        severity="info",
    )

    # rolltrader stable: no current errors
    AuditEvent.objects.create(
        event_name="rt.current.info.1",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(hours=1, minutes=5),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="rt.current.info.2",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(hours=2, minutes=5),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="rt.current.info.3",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(hours=3, minutes=5),
        severity="info",
    )

    AuditEvent.objects.create(
        event_name="rt.previous.error.1",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(hours=7, minutes=10),
        severity="error",
    )
    AuditEvent.objects.create(
        event_name="rt.previous.info.1",
        source_system="rolltrader_console",
        occurred_at=now - timedelta(hours=8, minutes=10),
        severity="info",
    )

    user = get_user_model().objects.create_user(
        username="health_staff_user",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)

    response = client.get(
        "/api/audit/events/health/",
        {"window_hours": EXPECTED_HEALTH_WINDOW_HOURS},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["window_hours"] == EXPECTED_HEALTH_WINDOW_HOURS
    assert payload["count"] == EXPECTED_PRODUCERS_COUNT
    assert payload["items"][0]["source_system"] == "extension"
    assert payload["items"][0]["score"] > payload["items"][1]["score"]


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_health_scores_non_staff_hides_private_debug_source(client: Client) -> None:
    """Non-staff health endpoint should not expose private debug-only sources."""
    now = timezone.now()
    visible_user = get_user_model().objects.create_user(
        username="health_visible_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )
    hidden_user = get_user_model().objects.create_user(
        username="health_hidden_user",
        password=TEST_PASSWORD,
        is_staff=False,
    )

    AuditEvent.objects.create(
        event_name="visible.health.info",
        source_system="extension",
        occurred_at=now - timedelta(hours=1),
        actor_id=str(visible_user.pk),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="hidden.health.debug",
        source_system="secret_source",
        occurred_at=now - timedelta(hours=1, minutes=30),
        actor_id=str(hidden_user.pk),
        club_id="club-private",
        severity="debug",
    )

    client.force_login(visible_user)
    response = client.get(
        "/api/audit/events/health/",
        {"window_hours": EXPECTED_HEALTH_WINDOW_HOURS},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    sources = [item["source_system"] for item in payload["items"]]
    assert "extension" in sources
    assert "secret_source" not in sources
