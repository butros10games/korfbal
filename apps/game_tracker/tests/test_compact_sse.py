"""Cross-runtime contracts and capacity invariants for compact public SSE."""

import asyncio
from copy import deepcopy
from http import HTTPStatus
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from django.test import override_settings
from loadtest.compact import CompactDecodeError, CompactDecoder
import pytest

from apps.game_tracker.adapters.outbound.compact_json import Dictionary
from apps.game_tracker.adapters.outbound.compact_match import (
    PUBLIC_RESOURCES,
    RESOURCE_NAMES,
    CompactMatch,
)
from apps.game_tracker.adapters.outbound.match_fanout import (
    Fanout,
    Mailbox,
    encode_event,
)
from apps.game_tracker.realtime.consumer import MatchEventsSseConsumer
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES


FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[6] / "fixtures/korfbal/compact-sse.json"
    ).read_text()
)


def packet(frame: dict[str, object]) -> list:
    """Read the actual SSE body, including its JSON transport envelope."""
    wire = frame["_wire"]
    assert isinstance(wire, bytes)
    return json.loads(wire.split(b"data: ", 1)[1])


def test_shared_wire_fixture_matches_python_encoder_and_independent_decoder() -> None:
    """Insert, edit, reorder, delete, statistics patches and mapping reset roundtrip."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    codec.epoch = "fixture-mapping-1"
    decoder = CompactDecoder(FIXTURE["match_id"])
    with patch(
        "apps.game_tracker.adapters.outbound.compact_match.time",
        return_value=1700000000,
    ):
        for index, sample in enumerate(FIXTURE["frames"]):
            if sample.get("reset"):
                codec.dictionary = Dictionary()
                codec.epoch = "fixture-mapping-2"
                codec.full = None
                frame = codec.snapshot()
            else:
                frame = (
                    codec.seed(sample["input"])
                    if index == 0
                    else codec.publish(sample["input"])
                )
            assert packet(frame) == sample["packet"]
            assert decoder.decode(packet(frame)) == sample["expected"]
    assert codec.document["public_reads"]["stats"]["attempts"] == 1
    assert "obsolete" not in codec.document["public_reads"]["stats"]
    assert len(codec.document["public_reads"]["shots"]["shots"]) == 1
    edited_time = 3456
    assert codec.document["public_reads"]["events"]["events"][1]["time"] == edited_time


def test_gap_discards_unconfirmed_resources_and_missing_base_can_be_reseeded() -> None:
    """Do not relabel stale live/timeline data after a missed broker revision."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    codec.seed(FIXTURE["frames"][0]["input"])
    codec.publish({
        "match_id": codec.match_id,
        "revision": 12,
        "resources": ["stats"],
        "public_reads": {"stats": {"attempts": 3}},
    })
    assert set(codec.document["public_reads"]) == {"stats"}
    assert "live" not in codec.document
    assert "events" in codec.missing()
    snapshot = deepcopy(FIXTURE["frames"][0]["input"])
    snapshot["revision"] = 12
    codec.seed(snapshot)
    assert not codec.missing()
    assert codec.document["public_reads"]["stats"] == {"attempts": 3}


def test_patch_gap_is_rejected_without_consuming_the_mapping() -> None:
    """A dropped frame requires a full reset; later patches cannot hide the loss."""
    decoder = CompactDecoder(FIXTURE["match_id"])
    frames = FIXTURE["frames"]
    decoder.decode(frames[0]["packet"])
    with pytest.raises(CompactDecodeError):
        decoder.decode(frames[2]["packet"])
    assert decoder.decode(frames[1]["packet"]) == frames[1]["expected"]
    assert decoder.decode(frames[-1]["packet"]) == frames[-1]["expected"]


def test_ten_thousand_compact_viewers_share_patch_and_reset_bytes() -> None:
    """Slow-viewer resets are encoded once per subscription group, never per socket."""
    worker = Fanout(AsyncMock())
    match_id = FIXTURE["match_id"]
    mask = frozenset({"events", "stats"})
    codec = CompactMatch(match_id, mask)
    worker.compact[match_id] = {mask: codec}
    boxes = [Mailbox((match_id,), resources=mask) for _ in range(10_000)]
    worker.subscribers[match_id] = set(boxes)
    worker.publish(FIXTURE["frames"][0]["input"])
    with patch.object(codec, "_frame", wraps=codec._frame) as encode:
        worker.publish(FIXTURE["frames"][1]["input"])
    expected_frames = 2
    assert encode.call_count == expected_frames
    assert len({id(box.pending[match_id]["_wire"]) for box in boxes}) == 1
    assert set(codec.document["public_reads"]) == {"events", "stats"}


def test_dictionary_overflow_falls_back_to_a_bounded_reset() -> None:
    """An oversized mapping must not grow forever or leave a partial dictionary."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    codec.seed(FIXTURE["frames"][0]["input"])
    with patch("apps.game_tracker.adapters.outbound.compact_json.MAX_DICTIONARY", 2):
        frame = codec.publish(FIXTURE["frames"][1]["input"])
    assert packet(frame)[4] is None
    assert codec.document == {"public_reads": {}}


@pytest.mark.parametrize(
    "query",
    [
        b"compact=2",
        b"compact=1&resources=tracker",
        b"compact=1&resources=stats&resources=live",
    ],
)
def test_subscription_rejects_unknown_protocol_or_private_payloads(
    query: bytes,
) -> None:
    """Subscription negotiation never exposes private tracker state."""
    consumer = MatchEventsSseConsumer()
    consumer.scope = {"query_string": query}
    with pytest.raises(ValueError, match=r"compact SSE version|public resource"):
        consumer._parse_resources()


def test_handshake_reseed_updates_existing_viewers_base() -> None:
    """Filling a missing resource for a new viewer must also reset existing viewers."""
    worker = Fanout(AsyncMock())
    match_id = FIXTURE["match_id"]
    mask = frozenset({"events", "stats"})
    codec = CompactMatch(match_id, mask)
    worker.compact[match_id] = {mask: codec}
    box = Mailbox((match_id,), resources=mask)
    worker.subscribers[match_id] = {box}
    initial = deepcopy(FIXTURE["frames"][0]["input"])
    initial["public_reads"].pop("stats")
    decoder = CompactDecoder(match_id)
    decoder.decode(packet(worker.seed_compact(match_id, mask, initial)))
    box.pending.clear()
    worker.seed_compact(match_id, mask, FIXTURE["frames"][0]["input"])
    assert decoder.decode(packet(box.pending.pop(match_id))) is not None
    worker.publish(FIXTURE["frames"][1]["input"])
    result = decoder.decode(packet(box.pending[match_id]))
    assert result is not None
    assert (
        result["public_reads"]["stats"]
        == FIXTURE["frames"][1]["expected"]["public_reads"]["stats"]
    )


def test_delayed_broker_event_cannot_rollback_a_newer_handshake() -> None:
    """A cache bootstrap can overtake broker delivery without rolling back the base."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    for frame in FIXTURE["frames"][:-1]:
        codec.seed(frame["input"]) if frame["input"].get("snapshot") else codec.publish(
            frame["input"]
        )
    previous = deepcopy(codec.document)
    sequence = codec.sequence
    codec.publish(FIXTURE["frames"][0]["input"])
    assert codec.document == previous
    assert codec.sequence == sequence


def test_resource_codes_stay_aligned_with_public_invalidation_contract() -> None:
    """Transport enums include invalidations for private resources, never their data."""
    assert set(RESOURCE_NAMES) == set(ALL_LIVE_RESOURCES)


def test_new_handshake_does_not_discard_a_cohort_before_publication_finishes() -> None:
    """A new connection seeing a committed revision must not cause a viewer stampede."""
    worker = Fanout(AsyncMock())
    match_id = FIXTURE["match_id"]
    codec = CompactMatch(match_id, PUBLIC_RESOURCES)
    worker.compact[match_id] = {PUBLIC_RESOURCES: codec}
    box = Mailbox((match_id,), resources=PUBLIC_RESOURCES)
    worker.subscribers[match_id] = {box}
    worker.seed_compact(match_id, PUBLIC_RESOURCES, FIXTURE["frames"][0]["input"])
    box.pending.clear()
    sequence = codec.sequence
    incomplete = {**FIXTURE["frames"][0]["input"], "revision": 11, "public_reads": {}}
    incomplete.pop("live", None)
    worker.seed_compact(match_id, PUBLIC_RESOURCES, incomplete)
    assert not box.pending
    assert codec.sequence == sequence
    assert not codec.missing()
    worker.publish(FIXTURE["frames"][1]["input"])
    assert codec.revision == incomplete["revision"]
    assert not codec.missing()


@pytest.mark.parametrize("legacy_viewers", [0, 1, 100])
def test_legacy_serialization_is_shared_and_only_done_when_needed(
    legacy_viewers: int,
) -> None:
    """Compact-only broadcasts skip full JSON; mixed clients retain legacy data."""
    worker = Fanout(AsyncMock())
    match_id = FIXTURE["match_id"]
    mask = frozenset({"events", "stats"})
    worker.compact[match_id] = {mask: CompactMatch(match_id, mask)}
    compact = Mailbox((match_id,), resources=mask)
    legacy = [Mailbox((match_id,)) for _ in range(legacy_viewers)]
    worker.subscribers[match_id] = {compact, *legacy}
    decoder = CompactDecoder(match_id)
    for index, sample in enumerate(FIXTURE["frames"][:2]):
        event = sample["input"]
        with patch(
            "apps.game_tracker.adapters.outbound.match_fanout.encode_event",
            wraps=encode_event,
        ) as encode:
            worker.publish(event)
        assert encode.call_count == bool(legacy_viewers)
        frame = compact.pending.pop(match_id)
        if index == 0:
            frame = worker.compact[match_id][mask].snapshot()
        decoded = decoder.decode(packet(frame))
        assert decoded is not None
        assert decoded["public_reads"] == {
            key: value
            for key, value in sample["expected"]["public_reads"].items()
            if key in mask
        }
        frames = [box.pending.pop(match_id) for box in legacy]
        if frames:
            assert len({id(frame["_wire"]) for frame in frames}) == 1
            assert frames[0]["_wire"] == encode_event(event)


def cursor(codec: CompactMatch) -> bytes:
    """Use the cursor emitted on the actual SSE frame."""
    return codec.snapshot()["_wire"].split(b"\n", 1)[0].removeprefix(b"id: ")


def test_reconnect_replays_missed_updates_into_existing_decoder() -> None:
    """Native EventSource retains its dictionary across a network reconnect."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    decoder = CompactDecoder(codec.match_id)
    decoder.decode(packet(codec.seed(FIXTURE["frames"][0]["input"])))
    previous = cursor(codec)
    assert codec.replay(previous) == []
    for sample in FIXTURE["frames"][1:3]:
        codec.publish(sample["input"])
    frames = codec.replay(previous)
    assert frames is not None
    assert len(frames) == len(FIXTURE["frames"][1:3])
    for wire in frames:
        result = decoder.decode(packet({"_wire": wire}))
    assert result == FIXTURE["frames"][2]["expected"]
    assert codec.replay(cursor(codec)) == []


@pytest.mark.parametrize("limit", ["MAX_REPLAY_FRAMES", "MAX_REPLAY_BYTES"])
def test_replay_eviction_requires_reset(limit: str) -> None:
    """Both memory and frame count cap shared retention, including oversized frames."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    codec.seed(FIXTURE["frames"][0]["input"])
    previous = cursor(codec)
    with patch(f"apps.game_tracker.adapters.outbound.compact_match.{limit}", 1):
        codec.publish(FIXTURE["frames"][1]["input"])
        recent = cursor(codec)
        codec.publish(FIXTURE["frames"][2]["input"])
    assert codec.replay(previous) is None
    if limit == "MAX_REPLAY_FRAMES":
        assert len(codec.replay(recent) or []) == 1
    else:
        assert codec.history_bytes == 0
        assert codec.replay(recent) is None


@pytest.mark.parametrize("invalid", [b"", b"bad", b"wrong:1", b":1", b"x" * 129])
def test_invalid_replay_cursor_falls_back(invalid: bytes) -> None:
    """Untrusted headers never prevent a full snapshot handshake."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    codec.seed(FIXTURE["frames"][0]["input"])
    assert codec.replay(invalid) is None
    assert codec.replay(f"{codec.epoch}:{codec.sequence + 1}".encode()) is None
    assert codec.replay(f"{codec.epoch}:-1".encode()) is None
    old = cursor(codec)
    codec._reset(codec.document)
    assert codec.replay(old) is None


def test_replay_includes_same_revision_resource_fill() -> None:
    """Projection completion advances the stream even without a new match revision."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    initial = deepcopy(FIXTURE["frames"][0]["input"])
    initial["public_reads"].pop("stats")
    decoder = CompactDecoder(codec.match_id)
    decoder.decode(packet(codec.seed(initial)))
    previous = cursor(codec)
    codec.seed(FIXTURE["frames"][0]["input"])
    frames = codec.replay(previous)
    assert frames is not None
    assert len(frames) == 1
    result = decoder.decode(packet({"_wire": frames[0]}))
    assert result is not None
    assert result["public_reads"]["stats"] == codec.document["public_reads"]["stats"]


def test_reconnect_discards_covered_mailbox_but_retains_later_updates() -> None:
    """Pending coalesced snapshots cannot undo replay savings or drop new delivery."""
    worker = Fanout(AsyncMock())
    match_id = FIXTURE["match_id"]
    codec = CompactMatch(match_id, PUBLIC_RESOURCES)
    worker.compact[match_id] = {PUBLIC_RESOURCES: codec}
    box = Mailbox((match_id,), resources=PUBLIC_RESOURCES)
    worker.subscribers[match_id] = {box}
    worker.seed_compact(match_id, PUBLIC_RESOURCES, FIXTURE["frames"][0]["input"])
    previous = cursor(codec)
    worker.publish(FIXTURE["frames"][1]["input"])
    consumer = MatchEventsSseConsumer()
    consumer.scope = {"headers": [(b"last-event-id", previous)]}
    consumer.fanout, consumer.mailbox = worker, box
    live, frames = consumer._compact_start(
        {match_id: codec.revision}, {}, PUBLIC_RESOURCES
    )
    assert not live
    assert len(frames) == 1
    assert packet({"_wire": frames[0]})[4] is not None
    assert not box.pending
    assert not box.changed.is_set()
    worker.publish(FIXTURE["frames"][2]["input"])
    assert box.changed.is_set()
    assert match_id in box.pending
    # Another resource cohort or an ahead-of-publication database revision resets.
    for header, revision in [
        (b"another-worker:1", codec.revision),
        (cursor(codec), codec.revision + 1),
    ]:
        consumer.scope = {"headers": [(b"last-event-id", header)]}
        _, frames = consumer._compact_start({match_id: revision}, {}, PUBLIC_RESOURCES)
        assert packet({"_wire": frames[0]})[4] is None


@pytest.mark.asyncio
@override_settings(KORFBAL_SSE_ENABLED=True)
@pytest.mark.parametrize("resume", [False, True])
async def test_http_reconnect_preserves_updates_arriving_during_replay_send(
    resume: bool,
) -> None:
    """The real handshake consumes Last-Event-ID and leaves later updates queued."""
    match_id = FIXTURE["match_id"]
    codec = CompactMatch(match_id, PUBLIC_RESOURCES)
    codec.seed(FIXTURE["frames"][0]["input"])
    previous = cursor(codec)
    codec.publish(FIXTURE["frames"][1]["input"])
    worker = Fanout(AsyncMock(return_value={match_id: codec.revision}))
    worker.compact[match_id] = {PUBLIC_RESOURCES: codec}
    consumer = MatchEventsSseConsumer()
    consumer.scope = {
        "query_string": f"match_ids={match_id}&compact=1".encode(),
        "headers": [(b"last-event-id", previous)] if resume else [],
    }
    consumer.subscription_ready = asyncio.Event()
    sent = []

    def subscribe(box: Mailbox) -> None:
        worker.subscribers[match_id] = {box}
        box.put(codec.snapshot())

    def send(message: dict) -> None:
        sent.append(message)
        if b"event: match.compact" in message.get("body", b""):
            worker.publish(FIXTURE["frames"][2]["input"])

    consumer.base_send = AsyncMock(side_effect=send)
    connected_at = 1800000040
    with (
        patch("apps.game_tracker.realtime.consumer.shared_fanout", return_value=worker),
        patch.object(worker, "subscribe", side_effect=subscribe),
        patch.object(worker.compact_bootstraps, "get", AsyncMock(return_value=None)),
        patch("apps.game_tracker.realtime.consumer.time", return_value=connected_at),
    ):
        try:
            await consumer.http_request({})
            assert sent[0]["status"] == HTTPStatus.OK
            ready = json.loads(sent[1]["body"].split(b"data: ", 1)[1])
            assert ready["server_time"] == connected_at
            assert ready["snapshot_matches"] == [match_id]
            assert set(ready["live_states"]) == (set() if resume else {match_id})
            assert (packet({"_wire": sent[2]["body"]})[4] is not None) == resume
            assert consumer.mailbox is not None
            assert consumer.mailbox.pending[match_id]["revision"] == codec.revision
            assert consumer.subscription_ready.is_set()
        finally:
            assert consumer.heartbeat_task is not None
            consumer.heartbeat_task.cancel()
            await asyncio.gather(consumer.heartbeat_task, return_exceptions=True)
            # Release the metric lease without starting/stopping a real broker worker.
            with patch.object(worker, "unsubscribe", AsyncMock()):
                await consumer._cleanup()


def test_multiplexed_reconnect_resets_other_matches_and_resource_masks() -> None:
    """A native EventSource cursor certifies only its last match/resource cohort."""
    match_id = FIXTURE["match_id"]
    other_id = "another-match"
    mask = frozenset({"events"})
    worker = Fanout(AsyncMock())
    for identity in (match_id, other_id):
        worker.compact[identity] = {}
        for resources in (mask, PUBLIC_RESOURCES):
            codec = CompactMatch(identity, resources)
            codec.seed({**FIXTURE["frames"][0]["input"], "match_id": identity})
            worker.compact[identity][resources] = codec
    current = worker.compact[match_id][PUBLIC_RESOURCES]
    consumer = MatchEventsSseConsumer()
    consumer.scope = {"headers": [(b"last-event-id", cursor(current))]}
    consumer.fanout = worker
    revisions = dict.fromkeys((match_id, other_id), current.revision)
    _, frames = consumer._compact_start(revisions, {}, PUBLIC_RESOURCES)
    assert len(frames) == 1
    assert packet({"_wire": frames[0]})[1] == other_id
    assert packet({"_wire": frames[0]})[4] is None
    _, frames = consumer._compact_start({match_id: current.revision}, {}, mask)
    assert len(frames) == 1
    assert packet({"_wire": frames[0]})[4] is None


def test_clock_only_changes_do_not_grow_dictionary_or_patch_document() -> None:
    """Existing decoders reconstruct the live clock from the frame timestamp."""
    codec = CompactMatch(FIXTURE["match_id"], PUBLIC_RESOURCES)
    decoder = CompactDecoder(codec.match_id)
    initial = deepcopy(FIXTURE["frames"][0]["input"])
    initial["live"]["timer"] = {
        "type": "active",
        "server_time": "2026-09-16T12:00:00+00:00",
        "time": "2026-09-16T11:55:00+00:00",
    }
    original = deepcopy(initial)
    with patch(
        "apps.game_tracker.adapters.outbound.compact_match.time",
        return_value=1700000000,
    ):
        result = decoder.decode(packet(codec.seed(initial)))
    assert initial == original
    assert result is not None
    assert result["live"]["timer"]["server_time"] == "2023-11-14T22:13:20+00:00"
    assert "server_time" not in codec.dictionary.strings
    size = len(codec.dictionary.strings)
    update = deepcopy(initial)
    update.update(snapshot=False, resources=["live"])
    update["live"]["timer"]["server_time"] = "2026-09-16T12:00:01+00:00"
    with patch(
        "apps.game_tracker.adapters.outbound.compact_match.time",
        return_value=1700000001,
    ):
        frame = packet(codec.publish(update))
    assert frame[8] == []
    assert frame[9] == []
    assert len(codec.dictionary.strings) == size
    result = decoder.decode(frame)
    assert result is not None
    assert result["live"]["timer"]["server_time"] == "2023-11-14T22:13:21+00:00"


def test_bootstrap_discards_its_already_covered_pending_snapshot() -> None:
    """A whole reconnect wave receives one bootstrap each instead of two."""
    worker = Fanout(AsyncMock())
    match_id = FIXTURE["match_id"]
    worker.compact[match_id] = {
        PUBLIC_RESOURCES: CompactMatch(match_id, PUBLIC_RESOURCES)
    }
    boxes = [Mailbox((match_id,), resources=PUBLIC_RESOURCES) for _ in range(100)]
    worker.subscribers[match_id] = set(boxes)
    worker.seed_compact(match_id, PUBLIC_RESOURCES, FIXTURE["frames"][0]["input"])
    for box in boxes:
        assert match_id in box.pending
        consumer = MatchEventsSseConsumer()
        consumer.scope = {}
        consumer.fanout, consumer.mailbox = worker, box
        _, frames = consumer._compact_start({match_id: 10}, {}, PUBLIC_RESOURCES)
        assert len(frames) == 1
        assert packet({"_wire": frames[0]})[4] is None
        assert not box.pending
        assert not box.changed.is_set()
    worker.publish(FIXTURE["frames"][1]["input"])
    assert all(match_id in box.pending for box in boxes)
