"""ASGI cancellation and bounded backpressure must survive a busy public stream."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from prometheus_client import REGISTRY
import pytest

from apps.game_tracker.adapters.outbound.match_fanout import Mailbox, encode_event
from apps.game_tracker.realtime.consumer import MatchEventsSseConsumer
from apps.tournament.realtime import TournamentEventsSseConsumer


class StreamHarness(MatchEventsSseConsumer):
    """Skip database handshakes while exercising the real connection lifecycle."""

    def __init__(self) -> None:
        """Expose an explicit handshake barrier to the lifecycle tests."""
        self.opened = asyncio.Event()

    async def http_request(self, event: dict[str, object]) -> None:
        """Expose a mailbox through the same ready barrier as the real handshake."""
        self.mailbox = Mailbox(("match",))
        self.subscription_ready.set()
        self.opened.set()


def change(revision: int) -> dict[str, Any]:
    """Return a complete shared frame suitable for the real sender."""
    event = {
        "type": "match.changed",
        "match_id": "match",
        "revision": revision,
        "resources": ["live"],
    }
    return {**event, "_wire": encode_event(event)}


async def start_stream(
    send: AsyncMock,
) -> tuple[StreamHarness, asyncio.Queue, asyncio.Task]:
    """Open the test stream and wait until its mailbox is available."""
    consumer = StreamHarness()
    incoming = asyncio.Queue()
    task = asyncio.create_task(consumer({}, incoming.get, send))
    incoming.put_nowait({"type": "http.request"})
    await asyncio.wait_for(consumer.opened.wait(), 1)
    return consumer, incoming, task


@pytest.mark.asyncio
async def test_disconnect_cancels_a_backpressured_send() -> None:
    """A blocked socket cannot prevent disconnect processing or retain its receiver."""
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_send(message: dict) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    consumer, incoming, task = await start_stream(AsyncMock(side_effect=blocked_send))
    with patch.object(consumer, "_cleanup", wraps=consumer._cleanup) as cleanup:
        try:
            assert consumer.mailbox is not None
            consumer.mailbox.put(change(1))
            await asyncio.wait_for(started.wait(), 1)
            incoming.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(asyncio.shield(task), 1)
            assert cancelled.is_set()
            cleanup.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_send_retains_only_the_latest_pending_match_frame() -> None:
    """Persistent senders preserve the bounded mailbox instead of draining ahead."""
    started, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    messages = []

    async def send(message: dict) -> None:
        messages.append(message)
        if len(messages) == 1:
            started.set()
            await release.wait()
        else:
            delivered.set()

    consumer, incoming, task = await start_stream(AsyncMock(side_effect=send))
    try:
        assert consumer.mailbox is not None
        consumer.mailbox.put(change(1))
        await asyncio.wait_for(started.wait(), 1)
        consumer.mailbox.put(change(2))
        consumer.mailbox.put(change(3))
        assert list(consumer.mailbox.pending) == ["match"]
        release.set()
        await asyncio.wait_for(delivered.wait(), 1)
        assert [message["body"] for message in messages] == [
            change(1)["_wire"],
            change(3)["_wire"],
        ]
    finally:
        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sender", "receiver", "parent"])
async def test_failed_or_cancelled_connection_releases_both_loops(failure: str) -> None:
    """Socket errors and server shutdown cancel pending work and run cleanup once."""
    send = AsyncMock(side_effect=OSError("closed") if failure == "sender" else None)
    consumer, incoming, task = await start_stream(send)
    with patch.object(consumer, "_cleanup", wraps=consumer._cleanup) as cleanup:
        if failure == "sender":
            assert consumer.mailbox is not None
            consumer.mailbox.put(change(1))
            expected = OSError
        elif failure == "receiver":
            incoming.put_nowait({"type": "unknown.event"})
            expected = AttributeError
        else:
            task.cancel()
            expected = asyncio.CancelledError
        with pytest.raises(expected):
            await asyncio.wait_for(task, 1)
        cleanup.assert_awaited_once()
        assert not consumer.match_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["match.changed", "heartbeat"])
@pytest.mark.parametrize("failed", [False, True])
async def test_delivery_counters_reuse_labels_and_count_only_successful_sends(
    event: str, failed: bool
) -> None:
    """Fixed-label caching preserves exported counts and excludes failed writes."""
    consumer = MatchEventsSseConsumer()
    consumer.base_send = AsyncMock(side_effect=OSError("closed") if failed else None)
    metric = "korfbal_sse_events_sent_total"
    before = REGISTRY.get_sample_value(metric, {"event": event})
    assert before is not None
    with (
        patch(
            "apps.game_tracker.realtime.consumer.SSE_EVENTS_SENT.labels",
            side_effect=AssertionError("Hot delivery path resolved a label"),
        ),
        patch(
            "apps.game_tracker.realtime.consumer.asyncio.sleep",
            side_effect=[None, asyncio.CancelledError],
        ),
    ):
        delivery = (
            consumer.match_changed(change(1))
            if event == "match.changed"
            else consumer._send_heartbeats()
        )
        if failed:
            with pytest.raises(OSError, match="closed"):
                await delivery
        else:
            await delivery
    assert REGISTRY.get_sample_value(metric, {"event": event}) == before + int(
        not failed
    )
    consumer.base_send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("tournament", [False, True])
async def test_heartbeat_is_a_minimal_sse_comment(tournament: bool) -> None:
    """Keep connections alive without generating a browser message or event ID."""
    consumer = TournamentEventsSseConsumer() if tournament else MatchEventsSseConsumer()
    send = AsyncMock(side_effect=asyncio.CancelledError)
    consumer.base_send = send
    with patch("asyncio.sleep", new_callable=AsyncMock):
        if isinstance(consumer, TournamentEventsSseConsumer):
            await consumer._heartbeats()
        else:
            await consumer._send_heartbeats()
    send.assert_awaited_once_with({
        "type": "http.response.body",
        "body": b":\n\n",
        "more_body": True,
    })
