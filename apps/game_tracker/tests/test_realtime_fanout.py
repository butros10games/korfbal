"""Shared broker reception, bounded backlogs and missed-message recovery."""

import asyncio
from unittest.mock import AsyncMock, patch

from django.test import override_settings
import pytest

from apps.game_tracker.adapters.outbound.match_fanout import (
    Fanout,
    Mailbox,
    encode_event,
    shared_fanout,
)


async def idle_receive(channel: str) -> None:
    """Keep the synthetic broker idle until cleanup cancels it."""
    await asyncio.sleep(3600)


def change(revision: int, resources: list[str]) -> dict:
    """Build a public notification without domain setup."""
    return {
        "type": "match.changed",
        "match_id": "match",
        "revision": revision,
        "resources": resources,
    }


@pytest.mark.asyncio
async def test_shared_subscription_and_bounded_slow_viewer() -> None:
    """Many viewers share one broker channel and cannot grow unbounded queues."""
    layer = AsyncMock()
    layer.new_channel.return_value = "local-channel"
    layer.receive.side_effect = idle_receive
    with patch(
        "apps.game_tracker.adapters.outbound.match_fanout.get_channel_layer",
        return_value=layer,
    ):
        reader = AsyncMock(return_value={})
        first, second = Mailbox(("match",)), Mailbox(("match",))
        worker = shared_fanout(reader)
        other = shared_fanout(reader)
        assert worker is other
        await worker.subscribe(first)
        await worker.subscribe(second)
        try:
            ready = await asyncio.gather(
                *(worker.current_revisions(("match",)) for _ in range(20))
            )
            assert ready == [{}] * 20
            reader.assert_awaited_once_with(("match",))
            layer.new_channel.assert_awaited_once()
            layer.group_add.assert_awaited_once()
            last_revision = 100
            for revision in range(1, last_revision + 1):
                worker.publish(change(revision, ["live" if revision == 1 else "stats"]))
                assert (await first.receive())["revision"] == revision
            assert len(second.pending) == 1
            latest = await second.receive()
            assert latest["revision"] == last_revision
            assert latest["resources"] == ["live", "stats"]
            worker.publish(change(99, ["shots"]))
            assert not first.pending
        finally:
            await worker.unsubscribe(first)
            layer.group_discard.assert_not_awaited()
            await worker.unsubscribe(second)
        layer.group_discard.assert_awaited_once()
        assert all(task.done() for task in worker.tasks)


@pytest.mark.asyncio
@override_settings(KORFBAL_SSE_RECONCILE_SECONDS=0.01)
@pytest.mark.parametrize("viewer_count", [100, 1000, 10_000])
async def test_recovery_reads_once_for_all_viewers(viewer_count: int) -> None:
    """A lost broker event recovers from durable revisions without per-viewer SQL."""
    with patch(
        "apps.game_tracker.adapters.outbound.match_fanout.get_channel_layer"
    ) as get_layer:
        layer = AsyncMock()
        layer.new_channel.return_value = "local-channel"
        layer.receive.side_effect = idle_receive
        get_layer.return_value = layer
        revision = 3
        reader = AsyncMock(return_value={"match": revision})
        boxes = [Mailbox(("match",)) for _ in range(viewer_count)]
        worker = shared_fanout(reader)
        await worker.subscribe(boxes[0])
        for box in boxes[1:]:
            shared_fanout(reader)
            await worker.subscribe(box)
        try:
            events = await asyncio.wait_for(
                asyncio.gather(*(box.receive() for box in boxes)), 1
            )
            assert all(
                event["revision"] == revision and "live" in event["resources"]
                for event in events
            )
            assert all(call.args == (("match",),) for call in reader.await_args_list)
        finally:
            for box in boxes:
                await worker.unsubscribe(box)


@pytest.mark.asyncio
async def test_coalesced_updates_do_not_relabel_old_deltas() -> None:
    """Dropped revisions retain invalidations but only the newest revision's data."""
    box = Mailbox(("match",))
    box.put({**change(3, ["events"]), "public_reads": {"events": {"base_revision": 2}}})
    box.put({**change(4, ["summary"]), "public_reads": {"summary": {"score": 2}}})
    event = await box.receive()
    assert event["resources"] == ["events", "summary"]
    assert event["public_reads"] == {"summary": {"score": 2}}
    assert b'"public_reads"' in event["_wire"]


def test_ten_thousand_slow_viewers_share_one_replacement_frame() -> None:
    """Serialization and retained frame bytes do not multiply with slow viewers."""
    worker = Fanout(AsyncMock())
    boxes = [Mailbox(("match",)) for _ in range(10_000)]
    worker.subscribers["match"] = set(boxes)
    worker.publish(change(1, ["events"]))
    with patch(
        "apps.game_tracker.adapters.outbound.match_fanout.encode_event",
        wraps=encode_event,
    ) as encode:
        worker.publish(change(2, ["summary"]))
    expected_frames = 2  # One ordinary frame and one shared replacement.
    assert encode.call_count == expected_frames
    retained = boxes[0].pending["match"]
    assert all(box.pending["match"] is retained for box in boxes)
    assert retained["resources"] == ["events", "summary"]
    assert all(len(box.pending) == 1 for box in boxes)


def test_replacement_variants_are_bounded_without_losing_invalidations() -> None:
    """A rare mixed backlog falls back to one shared superset after the limit."""
    worker = Fanout(AsyncMock())
    boxes = [Mailbox(("match",)) for _ in range(3)]
    resources = ["live", "events", "stats"]
    for box, resource in zip(boxes, resources, strict=True):
        box.put(change(1, [resource]))
    worker.subscribers["match"] = set(boxes)
    with patch(
        "apps.game_tracker.adapters.outbound.match_fanout.MAX_COALESCED_VARIANTS", 1
    ):
        worker.publish(change(2, ["summary"]))
    for box, resource in zip(boxes, resources, strict=True):
        assert {resource, "summary"} <= set(box.pending["match"]["resources"])
    expected_variants = 2
    assert len({id(box.pending["match"]) for box in boxes}) <= expected_variants


@pytest.mark.asyncio
async def test_reconciliation_gives_normal_publication_one_cycle() -> None:
    """A partially prepared cache must not turn a healthy update into refetches."""
    worker = Fanout(AsyncMock())
    box = Mailbox(("match",))
    worker.subscribers["match"] = {box}
    worker.latest["match"] = 1
    with patch.object(worker.bootstraps, "get", new=AsyncMock()) as read:
        await worker._recover("match", 2)
        assert not box.pending
        read.assert_not_awaited()
        worker.publish(change(2, ["stats"]))
        await worker._recover("match", 2)
        read.assert_not_awaited()
    assert (await box.receive())["resources"] == ["stats"]


@pytest.mark.asyncio
async def test_advancing_revisions_cannot_starve_lost_broker_recovery() -> None:
    """Once the grace cycle expires, recover the latest revision despite writes."""
    worker = Fanout(AsyncMock())
    box = Mailbox(("match",))
    worker.subscribers["match"] = {box}
    worker.latest["match"] = 1
    newest = 3
    with patch.object(
        worker.bootstraps, "get", new=AsyncMock(return_value=None)
    ) as read:
        await worker._recover("match", 2)
        await worker._recover("match", newest)
        read.assert_awaited_once_with("match", newest)
    assert (await box.receive())["revision"] == newest
