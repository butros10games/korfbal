"""Regression coverage for durable revisions and SSE delivery."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
import os
from unittest.mock import patch
from uuid import uuid4

from asgiref.testing import ApplicationCommunicator
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.test import override_settings
from loadtest.compact import CompactDecoder
import pytest

from apps.game_tracker.composition import (
    apply_tracker_command,
    prepare_public_match_reads,
)
from apps.game_tracker.realtime.consumer import MatchEventsSseConsumer
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.realtime.publisher import match_group_name
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
        "apps.game_tracker.adapters.outbound.runtime.publish_match_changed",
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
    tracker = await database_sync_to_async(create_tracker_match)(
        prefix="Realtime stream"
    )
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

    # Additional request-body frames must not acquire a second subscription.
    await communicator.send_input({
        "type": "http.request",
        "body": b"",
        "more_body": False,
    })
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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(KORFBAL_SSE_ENABLED=True)
async def test_committed_goal_delivers_public_snapshot_without_viewer_read() -> None:
    """The SSE frame itself carries the committed public score and clock."""
    tracker = await database_sync_to_async(create_tracker_match)(
        prefix="Pushed snapshot"
    )
    match_id = str(tracker.match.id_uuid)
    communicator = ApplicationCommunicator(
        MatchEventsSseConsumer.as_asgi(), _sse_scope(f"match_ids={match_id}".encode())
    )
    await communicator.send_input({
        "type": "http.request",
        "body": b"",
        "more_body": False,
    })
    await communicator.receive_output(timeout=1)
    await communicator.receive_output(timeout=1)
    try:
        state = await database_sync_to_async(apply_tracker_command)(
            tracker.match, team=tracker.home_team, payload={"command": "start/pause"}
        )
        changed = await communicator.receive_output(timeout=1)
        event = json.loads(changed["body"].split(b"data: ", 1)[1])
        assert event["live"]["match_id"] == match_id
        assert (
            event["live"]["live_revision"]
            == state["live_revision"]
            == event["revision"]
        )
        assert event["live"]["score"] == {"home": 0, "away": 0}
        assert set(event["public_reads"]) == set(event["resources"]) & {
            "summary",
            "stats",
            "events",
            "shots",
        }
        for resource in ("events", "shots"):
            if resource in event["public_reads"]:
                assert (
                    event["public_reads"][resource]["live_revision"]
                    == event["revision"]
                )
        assert set(event["live"]) == {
            "match_id",
            "match_data_id",
            "status",
            "current_part",
            "parts",
            "paused",
            "timer",
            "score",
            "last_changed_at",
            "live_revision",
        }
    finally:
        await communicator.send_input({"type": "http.disconnect"})
        await communicator.wait(timeout=1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_real_redis_idle_receiver_delivers_to_shared_viewers() -> None:
    """Real Redis idle receives survive the blocking-read interval and fan out."""
    url = os.environ.get("PUBLIC_LIVE_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set PUBLIC_LIVE_TEST_REDIS_URL to an isolated Redis database")
    match_id = str(uuid4())
    layer_settings = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [
                    {
                        "address": url,
                        "socket_timeout": None,
                        "socket_connect_timeout": 1,
                    }
                ],
                "prefix": f"fanout-test-{uuid4().hex}",
                "expiry": 10,
            },
        }
    }
    viewers = [
        ApplicationCommunicator(
            MatchEventsSseConsumer.as_asgi(),
            _sse_scope(f"match_ids={match_id}".encode()),
        )
        for _ in range(2)
    ]
    with (
        override_settings(
            KORFBAL_SSE_ENABLED=True,
            KORFBAL_SSE_RECONCILE_SECONDS=30,
            CHANNEL_LAYERS=layer_settings,
        ),
        patch.object(
            MatchEventsSseConsumer, "_current_revisions", return_value={match_id: 0}
        ),
        patch("apps.game_tracker.adapters.outbound.match_fanout.logger") as logger,
    ):
        try:
            for viewer in viewers:
                await viewer.send_input({
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                })
                await viewer.receive_output(timeout=2)
                await viewer.receive_output(timeout=2)
            await asyncio.sleep(6)
            layer = get_channel_layer()
            assert layer is not None
            await layer.group_send(
                match_group_name(match_id),
                {
                    "type": "match.changed",
                    "match_id": match_id,
                    "revision": 1,
                    "resources": ["live"],
                },
            )
            for viewer in viewers:
                changed = await viewer.receive_output(timeout=2)
                assert b'"revision":1' in changed["body"]
            logger.exception.assert_not_called()
        finally:
            for viewer in viewers:
                await viewer.send_input({"type": "http.disconnect"})
                await viewer.wait(timeout=2)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(KORFBAL_SSE_ENABLED=True)
@pytest.mark.parametrize("compact", [False, True])
async def test_opted_in_stream_sends_a_full_starting_snapshot(*, compact: bool) -> None:
    """An initial stream includes full timeline bases; old clients keep ready-only."""
    tracker = await database_sync_to_async(create_tracker_match)(prefix="SSE bootstrap")
    match_id = str(tracker.match.pk)
    await database_sync_to_async(prepare_public_match_reads)(match_id=match_id)
    suffix = "&compact=1&resources=events,shots" if compact else ""
    consumer = ApplicationCommunicator(
        MatchEventsSseConsumer.as_asgi(),
        _sse_scope(f"match_ids={match_id}&snapshot=1{suffix}".encode()),
    )
    await consumer.send_input({"type": "http.request", "body": b"", "more_body": False})
    try:
        await consumer.receive_output(timeout=1)
        ready = await consumer.receive_output(timeout=1)
        assert b'"snapshot_matches"' in ready["body"]
        message = await consumer.receive_output(timeout=1)
        payload = json.loads(message["body"].split(b"data: ", 1)[1])
        if compact:
            payload = CompactDecoder(match_id).decode(payload)
            assert payload is not None
            assert set(payload["public_reads"]) == {"events", "shots"}
            assert "live" not in payload
        assert payload["snapshot"] is True
        assert "events" in payload["public_reads"]["events"]
        assert "shots" in payload["public_reads"]["shots"]
    finally:
        await consumer.send_input({"type": "http.disconnect"})
        await consumer.wait(timeout=1)
