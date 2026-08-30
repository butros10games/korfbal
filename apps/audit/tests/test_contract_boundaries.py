"""Boundary and regression tests for the audit API contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from uuid import UUID

from django.contrib.auth.models import User
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.audit.models import AuditEvent


AUDIT_TOKEN = "boundary-secret"  # nosec
TEST_PASSWORD = "pass1234"  # nosec


@pytest.mark.django_db
@override_settings(KORFBAL_AUDIT_INGEST_TOKEN=AUDIT_TOKEN)
def test_individual_ingest_preserves_payload_and_attributes_authenticated_actor(
    client: Client,
) -> None:
    """Token ingestion should persist its contract and infer a missing actor."""
    user = User.objects.create_user(
        username="audit_ingest_user",
        password=TEST_PASSWORD,
    )
    client.force_login(user)
    occurred_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    response = client.post(
        "/api/audit/events/ingest/",
        data={
            "event_name": "match.updated",
            "occurred_at": occurred_at.isoformat(),
            "trace_id": "trace-123",
            "subject_type": "match",
            "subject_id": "match-456",
            "club_id": "club-789",
            "message": "Match updated",
            "metadata": {"origin": "web"},
            "payload": {"revision": 3},
        },
        content_type="application/json",
        headers={"X-Audit-Token": AUDIT_TOKEN},
    )

    assert response.status_code == HTTPStatus.CREATED
    event = AuditEvent.objects.get()
    response_payload = response.json()
    assert response_payload["id_uuid"] == str(event.id_uuid)
    assert datetime.fromisoformat(response_payload["occurred_at"]) == occurred_at
    assert event.occurred_at == occurred_at
    assert event.source_system == "unknown"
    assert event.severity == "info"
    assert event.actor_id == str(user.pk)
    assert event.actor_type == "django_user"
    assert event.trace_id == "trace-123"
    assert event.subject_type == "match"
    assert event.subject_id == "match-456"
    assert event.club_id == "club-789"
    assert event.message == "Match updated"
    assert event.metadata == {"origin": "web"}
    assert event.payload == {"revision": 3}
    assert event.ingested_via == "api"


@pytest.mark.django_db
@override_settings(KORFBAL_AUDIT_INGEST_TOKEN=AUDIT_TOKEN)
def test_bulk_ingest_requires_token(client: Client) -> None:
    """The bulk endpoint must enforce the same token boundary as single ingest."""
    response = client.post(
        "/api/audit/events/ingest/bulk/",
        data={"events": [{"event_name": "bulk.denied"}]},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {"detail": "Invalid audit ingest token."}
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
@override_settings(KORFBAL_AUDIT_INGEST_TOKEN=AUDIT_TOKEN)
def test_bulk_ingest_validation_is_atomic(client: Client) -> None:
    """One invalid batch item should prevent every item from being persisted."""
    response = client.post(
        "/api/audit/events/ingest/bulk/",
        data={
            "events": [
                {"event_name": "bulk.valid"},
                {"event_name": "bulk.invalid", "severity": "critical"},
            ]
        },
        content_type="application/json",
        headers={"X-Audit-Token": AUDIT_TOKEN},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize(
    "path",
    [
        "/api/audit/events/timeline/",
        "/api/audit/events/summary/",
        "/api/audit/events/producers/",
        "/api/audit/events/trends/",
        "/api/audit/events/health/",
    ],
)
@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_audit_read_endpoints_reject_anonymous_requests(
    client: Client,
    path: str,
) -> None:
    """Every audit read surface should return an API auth error without redirecting."""
    response = client.get(path)

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    assert response.headers["Content-Type"].startswith("application/json")
    assert "Location" not in response.headers


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_timeline_applies_exact_identity_and_inclusive_time_filters(
    client: Client,
) -> None:
    """Timeline filters should compose without admitting near-match events."""
    user = User.objects.create_user(
        username="audit_filter_staff",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)
    start = datetime(2026, 1, 2, 10, tzinfo=UTC)
    end = start + timedelta(hours=1)
    common = {
        "source_system": "django",
        "event_name": "match.updated",
        "actor_id": "actor-1",
        "club_id": "club-1",
        "severity": "info",
    }
    expected = AuditEvent.objects.create(occurred_at=start, **common)
    AuditEvent.objects.create(occurred_at=end, **common)
    AuditEvent.objects.create(
        occurred_at=start,
        **{**common, "actor_id": "actor-10"},
    )
    AuditEvent.objects.create(
        occurred_at=start,
        **{**common, "club_id": "club-10"},
    )
    AuditEvent.objects.create(
        occurred_at=start - timedelta(microseconds=1),
        **common,
    )

    response = client.get(
        "/api/audit/events/timeline/",
        {
            "source": "django",
            "event_name": "match.updated",
            "actor_id": "actor-1",
            "club_id": "club-1",
            "since": start.isoformat(),
            "until": start.isoformat(),
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert [item["id_uuid"] for item in response.json()["items"]] == [
        str(expected.id_uuid)
    ]


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_timeline_cursor_is_stable_when_timestamps_tie(client: Client) -> None:
    """The UUID tie-breaker should avoid gaps and duplicates between pages."""
    user = User.objects.create_user(
        username="audit_cursor_staff",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    client.force_login(user)
    occurred_at = timezone.now()
    expected_ids = [UUID(int=value) for value in (4, 3, 2, 1)]
    for event_id in reversed(expected_ids):
        AuditEvent.objects.create(
            id_uuid=event_id,
            occurred_at=occurred_at,
            event_name="cursor.tie",
            source_system="test",
        )

    first_response = client.get("/api/audit/events/timeline/", {"limit": 2})
    first_payload = first_response.json()
    second_response = client.get(
        "/api/audit/events/timeline/",
        {"limit": 2, "cursor": first_payload["next_cursor"]},
    )
    second_payload = second_response.json()

    assert first_response.status_code == HTTPStatus.OK
    assert second_response.status_code == HTTPStatus.OK
    assert [
        UUID(item["id_uuid"])
        for item in first_payload["items"] + second_payload["items"]
    ] == expected_ids
    assert first_payload["has_more"] is True
    assert second_payload["has_more"] is False
    assert second_payload["next_cursor"] is None
