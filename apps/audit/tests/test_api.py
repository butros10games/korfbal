"""Tests for audit ingestion and reporting APIs."""

from __future__ import annotations

from datetime import datetime, timedelta
from http import HTTPStatus

from django.contrib.auth.models import User
from django.test.client import Client
from django.utils import timezone
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import AuditEvent


AUDIT_TOKEN = "top-secret"
AUDIT_API = "/api/audit/events"
pytestmark = pytest.mark.django_db


def _login(client: Client, username: str = "viewer", *, staff: bool = False) -> str:
    """Create and log in a passwordless local user, returning its actor ID."""
    user = User.objects.create(username=username, is_staff=staff)
    client.force_login(user)
    return str(user.pk)


def _event(
    name: str,
    *,
    occurred_at: datetime,
    source: str = "extension",
    severity: str = "info",
    visibility: dict[str, str] | None = None,
) -> AuditEvent:
    visibility = visibility or {}
    return AuditEvent.objects.create(
        event_name=name,
        source_system=source,
        occurred_at=occurred_at,
        severity=severity,
        actor_id=visibility.get("actor_id", ""),
        club_id=visibility.get("club_id", ""),
    )


def _event_table(
    now: datetime,
    rows: list[tuple[str, str, timedelta, str]],
) -> None:
    """Insert name/source/age/severity rows without repeating model setup."""
    for name, source, age, severity in rows:
        _event(
            name,
            source=source,
            occurred_at=now - age,
            severity=severity,
        )


def _visibility_events(*, actor_id: str) -> None:
    """Create one event for each non-staff visibility branch."""
    now = timezone.now()
    _event(
        "visible.actor.debug",
        source="actor-source",
        occurred_at=now - timedelta(hours=4),
        severity="debug",
        visibility={"actor_id": actor_id, "club_id": "private-club"},
    )
    _event(
        "visible.blank-club.debug",
        source="blank-club-source",
        occurred_at=now - timedelta(hours=3),
        severity="debug",
        visibility={"actor_id": "other-user"},
    )
    _event(
        "visible.allowed-severity",
        source="allowed-source",
        occurred_at=now - timedelta(hours=2),
        severity="error",
        visibility={"actor_id": "other-user", "club_id": "private-club"},
    )
    _event(
        "hidden.private.debug",
        source="private-source",
        occurred_at=now - timedelta(hours=1),
        severity="debug",
        visibility={"actor_id": "other-user", "club_id": "private-club"},
    )


@pytest.mark.parametrize(
    ("configured_token", "request_token", "expected_status"),
    [
        pytest.param("", None, HTTPStatus.FORBIDDEN, id="unconfigured"),
        pytest.param(AUDIT_TOKEN, None, HTTPStatus.FORBIDDEN, id="missing"),
        pytest.param(AUDIT_TOKEN, "wrong", HTTPStatus.FORBIDDEN, id="wrong"),
        pytest.param(AUDIT_TOKEN, AUDIT_TOKEN, HTTPStatus.CREATED, id="matching"),
    ],
)
def test_ingest_authentication(
    client: Client,
    settings: SettingsWrapper,
    configured_token: str,
    request_token: str | None,
    expected_status: HTTPStatus,
) -> None:
    """Fail closed unless the ingest request carries the configured token."""
    settings.KORFBAL_AUDIT_INGEST_TOKEN = configured_token
    headers = {"X-Audit-Token": request_token} if request_token else {}
    response = client.post(
        f"{AUDIT_API}/ingest/",
        data={"event_name": "trade.updated", "source_system": "cli"},
        content_type="application/json",
        headers=headers,
    )

    assert response.status_code == expected_status
    assert AuditEvent.objects.exists() is (expected_status == HTTPStatus.CREATED)


def test_ingest_persists_normalized_event(
    client: Client,
    settings: SettingsWrapper,
) -> None:
    """Persist the normalized event fields returned by single ingestion."""
    settings.KORFBAL_AUDIT_INGEST_TOKEN = AUDIT_TOKEN
    response = client.post(
        f"{AUDIT_API}/ingest/",
        data={
            "event_name": "trade.failed",
            "source_system": "cli",
            "severity": "error",
            "message": "rejected",
        },
        content_type="application/json",
        headers={"X-Audit-Token": AUDIT_TOKEN},
    )

    assert response.status_code == HTTPStatus.CREATED
    event = AuditEvent.objects.get(id_uuid=response.json()["id_uuid"])
    assert (event.event_name, event.source_system, event.severity) == (
        "trade.failed",
        "cli",
        "error",
    )
    assert event.message == "rejected"
    assert event.ingested_via == "api"


def test_bulk_ingest_persists_every_event(
    client: Client,
    settings: SettingsWrapper,
) -> None:
    """Persist every normalized event in a bulk request."""
    settings.KORFBAL_AUDIT_INGEST_TOKEN = AUDIT_TOKEN
    response = client.post(
        f"{AUDIT_API}/ingest/bulk/",
        data={
            "events": [
                {"event_name": "cli.start", "source_system": "console"},
                {
                    "event_name": "cli.finish",
                    "source_system": "console",
                    "severity": "warning",
                },
            ]
        },
        content_type="application/json",
        headers={"X-Audit-Token": AUDIT_TOKEN},
    )

    assert response.status_code == HTTPStatus.CREATED
    stored = list(
        AuditEvent.objects.order_by("event_name").values(
            "id_uuid", "event_name", "source_system", "severity"
        )
    )
    expected_rows = [
        ("cli.finish", "console", "warning"),
        ("cli.start", "console", "info"),
    ]
    assert response.json()["created"] == len(stored) == len(expected_rows)
    assert len(response.json()["ids"]) == len(expected_rows)
    assert set(response.json()["ids"]) == {str(row["id_uuid"]) for row in stored}
    assert [
        (row["event_name"], row["source_system"], row["severity"]) for row in stored
    ] == expected_rows


@pytest.mark.parametrize(
    "endpoint",
    ["timeline", "summary", "producers", "trends", "health"],
)
def test_reporting_endpoints_require_authentication(
    client: Client,
    endpoint: str,
) -> None:
    """Protect every reporting endpoint from unauthenticated reads."""
    response = client.get(f"{AUDIT_API}/{endpoint}/")
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_timeline_filters_and_orders_newest_first(client: Client) -> None:
    """Combine source/search filters and return matching events newest first."""
    now = timezone.now()
    _event_table(
        now,
        [
            ("sync.started", "extension", timedelta(minutes=10), "info"),
            ("sync.completed", "extension", timedelta(0), "info"),
            ("trade.extension", "extension", timedelta(minutes=2), "info"),
            ("sync.cli", "cli", timedelta(minutes=3), "info"),
            ("trade.created", "cli", timedelta(minutes=5), "warning"),
        ],
    )
    _login(client, staff=True)
    response = client.get(
        f"{AUDIT_API}/timeline/",
        {"source": "extension", "search": "sync", "limit": 10},
    )

    assert response.status_code == HTTPStatus.OK
    assert [item["event_name"] for item in response.json()["items"]] == [
        "sync.completed",
        "sync.started",
    ]


@pytest.mark.parametrize(
    ("event_name", "visible"),
    [
        pytest.param("visible.actor.debug", True, id="actor-debug"),
        pytest.param("visible.blank-club.debug", True, id="blank-club-debug"),
        pytest.param("visible.allowed-severity", True, id="allowed-severity-private"),
        pytest.param("hidden.private.debug", False, id="private-debug"),
    ],
)
def test_timeline_applies_each_non_staff_visibility_rule(
    client: Client,
    event_name: str,
    visible: bool,
) -> None:
    """Exercise one independently named non-staff visibility predicate."""
    actor_id = _login(client)
    _visibility_events(actor_id=actor_id)
    response = client.get(f"{AUDIT_API}/timeline/")

    assert response.status_code == HTTPStatus.OK
    returned_names = {item["event_name"] for item in response.json()["items"]}
    assert (event_name in returned_names) is visible


def test_timeline_cursor_pages_are_disjoint(client: Client) -> None:
    """Return a usable cursor whose next page contains no repeated rows."""
    now = timezone.now()
    _event_table(
        now,
        [
            (f"event.{index}", "extension", timedelta(minutes=index), "info")
            for index in range(5)
        ],
    )
    _login(client, staff=True)
    page_size = 2
    first = client.get(f"{AUDIT_API}/timeline/", {"limit": page_size})
    assert first.status_code == HTTPStatus.OK
    assert first.json()["count"] == page_size
    assert first.json()["has_more"] is True
    assert first.json()["next_cursor"]

    second = client.get(
        f"{AUDIT_API}/timeline/",
        {"limit": page_size, "cursor": first.json()["next_cursor"]},
    )

    assert second.status_code == HTTPStatus.OK
    assert [row["event_name"] for row in first.json()["items"]] == [
        "event.0",
        "event.1",
    ]
    assert [row["event_name"] for row in second.json()["items"]] == [
        "event.2",
        "event.3",
    ]


def test_timeline_rejects_invalid_cursor(client: Client) -> None:
    """Return a client error for malformed timeline cursors."""
    _login(client, staff=True)
    response = client.get(f"{AUDIT_API}/timeline/", {"cursor": "not-a-cursor"})
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_summary_returns_staff_aggregates(client: Client) -> None:
    """Group a staff-visible window by severity, producer, and event name."""
    now = timezone.now()
    _event_table(
        now,
        [
            ("sync.started", "extension", timedelta(hours=1), "info"),
            ("sync.started", "extension", timedelta(minutes=10), "warning"),
            ("trade.failed", "console", timedelta(minutes=5), "error"),
        ],
    )
    _login(client, staff=True)
    window_hours = 24
    response = client.get(f"{AUDIT_API}/summary/", {"window_hours": window_hours})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    expected_severities = {"error": 1, "info": 1, "warning": 1}
    assert payload["window_hours"] == window_hours
    assert payload["total"] == sum(expected_severities.values())
    assert payload["by_severity"] == expected_severities
    assert {row["source_system"]: row["count"] for row in payload["by_source"]} == {
        "extension": 2,
        "console": 1,
    }
    assert {row["event_name"]: row["count"] for row in payload["top_events"]} == {
        "sync.started": 2,
        "trade.failed": 1,
    }


def test_producer_stats_group_totals_and_errors(client: Client) -> None:
    """Group producer totals, severities, and last-seen timestamps."""
    now = timezone.now()
    _event_table(
        now,
        [
            ("a", "extension", timedelta(minutes=20), "info"),
            ("b", "extension", timedelta(minutes=5), "error"),
            ("c", "console", timedelta(minutes=2), "warning"),
        ],
    )
    _login(client, staff=True)
    response = client.get(f"{AUDIT_API}/producers/", {"window_hours": 24})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    by_source = {row["source_system"]: row for row in payload["items"]}
    expected_metrics = {"extension": (2, 1, 0), "console": (1, 0, 1)}
    assert payload["count"] == len(expected_metrics)
    metrics = {
        source: tuple(row[key] for key in ("total", "errors", "warnings"))
        for source, row in by_source.items()
    }
    assert metrics == expected_metrics
    assert all(row["last_seen"] is not None for row in by_source.values())


def test_trends_returns_hourly_points_and_error_rate_delta(client: Client) -> None:
    """Compare current hourly error rates with the preceding window."""
    now = timezone.now()
    _event_table(
        now,
        [
            ("current.info.1", "extension", timedelta(hours=1), "info"),
            ("current.info.2", "extension", timedelta(hours=2), "info"),
            ("current.warning", "console", timedelta(hours=3), "warning"),
            ("current.error", "console", timedelta(hours=4), "error"),
            ("previous.error", "extension", timedelta(hours=7), "error"),
            ("previous.info", "extension", timedelta(hours=8), "info"),
        ],
    )
    _login(client, staff=True)
    window_hours = 6
    response = client.get(f"{AUDIT_API}/trends/", {"window_hours": window_hours})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["window_hours"] == window_hours
    assert len(payload["points"]) == window_hours + 1
    assert payload["error_rate"] == {
        "current": 25.0,
        "previous": 50.0,
        "delta": -25.0,
    }


def test_health_ranks_worst_producer_first(client: Client) -> None:
    """Rank a worsening producer above a healthy producer."""
    now = timezone.now()
    _event_table(
        now,
        [
            ("ext.error.1", "extension", timedelta(hours=1), "error"),
            ("ext.error.2", "extension", timedelta(hours=2), "error"),
            ("ext.info", "extension", timedelta(hours=2, minutes=10), "info"),
            ("ext.warning", "extension", timedelta(hours=3), "warning"),
            ("ext.previous.1", "extension", timedelta(hours=7), "info"),
            ("ext.previous.2", "extension", timedelta(hours=8), "info"),
            ("cli.info.1", "console", timedelta(hours=1), "info"),
            ("cli.info.2", "console", timedelta(hours=2), "info"),
            ("cli.info.3", "console", timedelta(hours=3), "info"),
            ("cli.previous.error", "console", timedelta(hours=7), "error"),
            ("cli.previous.info", "console", timedelta(hours=8), "info"),
        ],
    )
    _login(client, staff=True)
    window_hours = 6
    response = client.get(f"{AUDIT_API}/health/", {"window_hours": window_hours})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    expected_sources = [
        "extension",
        "console",
    ]
    assert payload["window_hours"] == window_hours
    assert payload["count"] == len(expected_sources)
    assert [row["source_system"] for row in payload["items"]] == expected_sources
    assert payload["items"][0]["score"] > payload["items"][1]["score"]


@pytest.mark.parametrize("endpoint", ["summary", "producers", "trends", "health"])
def test_aggregate_endpoints_apply_non_staff_visibility(
    client: Client,
    endpoint: str,
) -> None:
    """Apply the same non-staff visibility boundary to every aggregate."""
    actor_id = _login(client)
    _visibility_events(actor_id=actor_id)
    response = client.get(f"{AUDIT_API}/{endpoint}/", {"window_hours": 24})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    visible_names = {
        "visible.actor.debug",
        "visible.blank-club.debug",
        "visible.allowed-severity",
    }
    visible_sources = {"actor-source", "blank-club-source", "allowed-source"}
    if endpoint == "summary":
        assert payload["total"] == len(visible_names)
        assert {row["event_name"] for row in payload["top_events"]} == visible_names
    elif endpoint == "trends":
        assert [
            point["by_severity"] for point in payload["points"] if point["total"]
        ] == [
            {"debug": 1, "info": 0, "warning": 0, "error": 0},
            {"debug": 1, "info": 0, "warning": 0, "error": 0},
            {"debug": 0, "info": 0, "warning": 0, "error": 1},
        ]
    else:
        assert payload["count"] == len(visible_sources)
        assert {row["source_system"] for row in payload["items"]} == visible_sources
