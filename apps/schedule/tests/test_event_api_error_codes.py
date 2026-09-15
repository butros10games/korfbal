"""Regression cases for distinguishing invalid event input from missing resources."""

from http import HTTPStatus

from django.test import Client
import pytest

from apps.game_tracker.models import MatchEvent
from apps.schedule.tests.match_api_test_support import (
    create_editor_context,
    goal_payload,
)


pytestmark = pytest.mark.django_db
MISSING_ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize(
    "suffix",
    [
        "goals/invalid/",
        "substitutes/invalid/",
        "pauses/invalid/",
        "timeouts/invalid/",
        "possession-changes/invalid/",
        "reconciliations/invalid/resolve/",
    ],
)
def test_malformed_event_identifier_is_not_found(client: Client, suffix: str) -> None:
    """Malformed nested UUID paths never reach ORM lookups."""
    context = create_editor_context(client, username="invalid-event-id")
    method = client.post if "resolve" in suffix else client.delete
    response = method(
        f"/api/matches/{context.graph.match.pk}/events/{suffix}",
        data={"expected_revision": context.graph.match_data.live_revision},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"decision": "unknown"}, HTTPStatus.BAD_REQUEST),
        (
            {"decision": "merge", "canonical_event_id": "invalid"},
            HTTPStatus.BAD_REQUEST,
        ),
        ({"decision": "merge", "reason": "a" * 256}, HTTPStatus.BAD_REQUEST),
        ({"decision": "separate"}, HTTPStatus.NOT_FOUND),
    ],
)
def test_reconciliation_distinguishes_bad_input_and_missing_candidate(
    client: Client, payload: dict, expected_status: int
) -> None:
    """Neither invalid decisions nor missing candidates are concurrency conflicts."""
    context = create_editor_context(client, username="invalid-reconciliation")
    response = client.post(
        f"/api/matches/{context.graph.match.pk}/events/reconciliations/{MISSING_ID}/resolve/",
        data=payload,
        content_type="application/json",
    )
    assert response.status_code == expected_status
    assert response.json()["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"time": "2025-02-30T12:00:00Z"},
        {"time": "0001-01-01T00:00:00+23:59"},
        {"minute": 10**30},
    ],
)
def test_invalid_event_time_is_bad_request_without_mutation(
    client: Client, overrides: dict
) -> None:
    """Calendar errors and timedelta overflow must not become server failures."""
    context = create_editor_context(client, username="invalid-event-time")
    before = MatchEvent.objects.filter(match_data=context.graph.match_data).count()
    data = goal_payload(
        context, expected_revision=context.graph.match_data.live_revision, **overrides
    )
    response = client.post(
        f"/api/matches/{context.graph.match.pk}/events/goals/",
        data=data,
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["code"] == "bad_request"
    assert (
        MatchEvent.objects.filter(match_data=context.graph.match_data).count() == before
    )


@pytest.mark.parametrize("kind", ["pauses", "timeouts"])
def test_excessive_pause_length_is_bad_request(client: Client, kind: str) -> None:
    """Duration arithmetic must reject extreme client input before writing an event."""
    context = create_editor_context(client, username="invalid-pause-length")
    response = client.post(
        f"/api/matches/{context.graph.match.pk}/events/{kind}/",
        data={
            "match_part_id": str(context.match_part.pk),
            "team_id": str(context.graph.home_team.pk),
            "minute": 0,
            "length_seconds": 10**30,
            "expected_revision": context.graph.match_data.live_revision,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "length_seconds" in response.json()
