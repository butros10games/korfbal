"""Regression coverage for durable revisions and SSE delivery."""

from __future__ import annotations

from http import HTTPStatus
import json
from unittest.mock import patch

from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from channels.layers import get_channel_layer
from django.test import override_settings
import pytest

from apps.game_tracker.realtime.consumer import MatchEventsSseConsumer
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.realtime.publisher import match_group_name
from apps.game_tracker.services.tracker_http import apply_tracker_command
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


def _sse_scope(query_string: bytes = b"") -> dict[str, object]:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/live/events/",
        "raw_path": b"/api/live/events/",
        "query_string": query_string,
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }


@pytest.mark.django_db(transaction=True)
def test_committed_revision_publishes_affected_resources() -> None:
    """A committed tracker command publishes its durable revision once."""
    tracker = create_tracker_match(prefix="Realtime publication")

    with patch(
        "apps.game_tracker.services.live_updates.publish_match_changed",
    ) as publish:
        state = apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "start/pause"},
        )

    publish.assert_called_once()
    assert publish.call_args.kwargs["revision"] == state["live_revision"]
    assert LiveResource.LIVE in publish.call_args.kwargs["resources"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(KORFBAL_SSE_ENABLED=True)
async def test_sse_consumer_sends_ready_and_match_change_events() -> None:
    """A group notification is forwarded on an established SSE stream."""
    tracker = await sync_to_async(create_tracker_match)(prefix="Realtime stream")
    match_id = str(tracker.match.id_uuid)
    application = MatchEventsSseConsumer.as_asgi()
    communicator = ApplicationCommunicator(
        application,
        _sse_scope(f"match_ids={match_id}".encode()),
    )

    await communicator.send_input(
        {"type": "http.request", "body": b"", "more_body": False},
    )
    response_start = await communicator.receive_output(timeout=1)
    ready = await communicator.receive_output(timeout=1)

    assert response_start["status"] == HTTPStatus.OK
    assert b"event: ready" in ready["body"]
    assert json.loads(ready["body"].split(b"data: ", maxsplit=1)[1])["revisions"] == {
        match_id: 0,
    }

    channel_layer = get_channel_layer()
    assert channel_layer is not None
    await channel_layer.group_send(
        match_group_name(match_id),
        {
            "type": "match.changed",
            "match_id": match_id,
            "revision": 1,
            "resources": ["live"],
        },
    )
    changed = await communicator.receive_output(timeout=1)
    assert b"event: match.changed" in changed["body"]
    assert b'"revision":1' in changed["body"]

    await communicator.send_input({"type": "http.disconnect"})
    await communicator.wait(timeout=1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(KORFBAL_SSE_ENABLED=False)
async def test_sse_consumer_is_disabled_by_default() -> None:
    """The rollout flag rejects streams before joining channel groups."""
    communicator = ApplicationCommunicator(
        MatchEventsSseConsumer.as_asgi(),
        _sse_scope(b"match_ids=00000000-0000-0000-0000-000000000001"),
    )

    with patch("apps.game_tracker.realtime.consumer.SSE_REJECTIONS") as rejections:
        await communicator.send_input(
            {"type": "http.request", "body": b"", "more_body": False},
        )
        response_start = await communicator.receive_output(timeout=1)
        response_body = await communicator.receive_output(timeout=1)

    assert response_start["status"] == HTTPStatus.NOT_FOUND
    assert json.loads(response_body["body"]) == {"detail": "SSE is disabled."}
    rejections.labels.assert_called_once_with(reason="disabled")
