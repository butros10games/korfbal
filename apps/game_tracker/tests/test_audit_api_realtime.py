"""Audit coverage for the public match SSE request contract."""

from __future__ import annotations

from http import HTTPStatus
import json
from unittest.mock import patch
from uuid import UUID

from asgiref.testing import ApplicationCommunicator
from django.test import override_settings
import pytest

from apps.game_tracker.realtime.consumer import MatchEventsSseConsumer


FIRST_MATCH_ID = "00000000-0000-0000-0000-000000000001"
SECOND_MATCH_ID = "00000000-0000-0000-0000-000000000002"

pytestmark = pytest.mark.django_db(transaction=True)


def _scope(*, query_string: bytes, origin: bytes | None = None) -> dict[str, object]:
    headers = [] if origin is None else [(b"origin", origin)]
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/live/events/",
        "raw_path": b"/api/live/events/",
        "query_string": query_string,
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }


async def _open(scope: dict[str, object]) -> tuple[ApplicationCommunicator, dict]:
    communicator = ApplicationCommunicator(
        MatchEventsSseConsumer.as_asgi(),
        scope,
    )
    await communicator.send_input(
        {"type": "http.request", "body": b"", "more_body": False},
    )
    response = await communicator.receive_output(timeout=1)
    return communicator, response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_string", "detail"),
    [
        (b"", "At least one match_id is required."),
        (b"match_ids=not-a-uuid", "Every match_id must be a valid UUID."),
        (
            f"match_ids={FIRST_MATCH_ID},{SECOND_MATCH_ID}".encode(),
            "Too many match_ids were requested.",
        ),
    ],
)
@override_settings(KORFBAL_SSE_ENABLED=True, KORFBAL_SSE_MAX_MATCHES=1)
async def test_sse_rejects_invalid_match_subscriptions(
    query_string: bytes,
    detail: str,
) -> None:
    """Malformed or oversized subscriptions fail as bounded JSON responses."""
    communicator, response_start = await _open(_scope(query_string=query_string))
    response_body = await communicator.receive_output(timeout=1)

    assert response_start["status"] == HTTPStatus.BAD_REQUEST
    assert response_start["headers"] == [(b"content-type", b"application/json")]
    assert json.loads(response_body["body"]) == {"detail": detail}
    assert response_body["more_body"] is False
    await communicator.wait(timeout=1)


@pytest.mark.asyncio
@override_settings(
    KORFBAL_SSE_ENABLED=True,
    CORS_ALLOW_ALL_ORIGINS=False,
    CORS_ALLOWED_ORIGINS=["https://trusted.example"],
)
async def test_sse_rejects_untrusted_origin_before_subscribing() -> None:
    """Browser origins outside the allowlist cannot join public match groups."""
    communicator, response_start = await _open(
        _scope(
            query_string=f"match_ids={FIRST_MATCH_ID}".encode(),
            origin=b"https://attacker.example",
        ),
    )
    response_body = await communicator.receive_output(timeout=1)

    assert response_start["status"] == HTTPStatus.FORBIDDEN
    assert json.loads(response_body["body"]) == {"detail": "Origin is not allowed."}
    await communicator.wait(timeout=1)


@pytest.mark.asyncio
@override_settings(
    KORFBAL_SSE_ENABLED=True,
    KORFBAL_SSE_MAX_MATCHES=2,
    CORS_ALLOW_ALL_ORIGINS=False,
    CORS_ALLOWED_ORIGINS=["https://trusted.example"],
)
async def test_sse_canonicalizes_duplicates_and_echoes_allowed_origin() -> None:
    """Duplicate UUIDs create one subscription and trusted CORS is explicit."""
    upper_match_id = str(UUID(FIRST_MATCH_ID)).upper()
    scope = _scope(
        query_string=(
            f"match_ids={upper_match_id},%20{FIRST_MATCH_ID}%20,{upper_match_id}"
        ).encode(),
        origin=b"https://trusted.example",
    )

    with patch.object(
        MatchEventsSseConsumer,
        "_current_revisions",
        return_value={},
    ) as current_revisions:
        communicator, response_start = await _open(scope)
        ready = await communicator.receive_output(timeout=1)

    headers = dict(response_start["headers"])
    assert response_start["status"] == HTTPStatus.OK
    assert headers[b"access-control-allow-origin"] == b"https://trusted.example"
    assert headers[b"cache-control"] == b"no-cache, no-transform"
    assert b"event: ready" in ready["body"]
    current_revisions.assert_awaited_once_with((FIRST_MATCH_ID,))

    await communicator.send_input({"type": "http.disconnect"})
    await communicator.wait(timeout=1)
