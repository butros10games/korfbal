"""Cross-worker compact replay and failure-boundary regression coverage."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
from threading import Barrier, local
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.test import override_settings
from loadtest.compact import CompactDecoder
import pytest

from apps.game_tracker.adapters.outbound.compact_match import (
    PUBLIC_RESOURCES,
    CompactMatch,
)
from apps.game_tracker.adapters.outbound.match_fanout import Fanout, Mailbox
from apps.game_tracker.adapters.outbound.shared_compact import (
    SharedCompactStore,
    SharedCompactUnavailableError,
)


FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[6] / "fixtures/korfbal/compact-sse.json"
    ).read_text()
)


def event(index: int, match_id: str) -> dict:
    """Give every case an isolated public match namespace."""
    return {**deepcopy(FIXTURE["frames"][index]["input"]), "match_id": match_id}


def decode(decoder: CompactDecoder, wire: bytes) -> dict | None:
    """Decode actual SSE bytes using the independent client implementation."""
    return decoder.decode(json.loads(wire.split(b"data: ", 1)[1]))


def worker(match_id: str) -> tuple[Fanout, Mailbox]:
    """Create a separate serving worker without starting an external broker loop."""
    fanout = Fanout(AsyncMock())
    box = Mailbox((match_id,), resources=PUBLIC_RESOURCES)
    fanout.subscribers[match_id] = {box}
    fanout.compact[match_id] = {
        PUBLIC_RESOURCES: CompactMatch(match_id, PUBLIC_RESOURCES)
    }
    return fanout, box


@pytest.mark.asyncio
async def test_reconnect_to_another_worker_replays_and_continues_live_updates() -> None:
    """Different and replacement workers preserve the same dictionary and sequence."""
    match_id = str(uuid4())
    first, first_box = worker(match_id)
    await first.prepare_compact(match_id, PUBLIC_RESOURCES, event(0, match_id))
    original = first.compact[match_id][PUBLIC_RESOURCES]
    decoder = CompactDecoder(match_id)
    decode(decoder, original.snapshot()["_wire"])
    cursor = f"{original.epoch}:{original.sequence}".encode()
    first_box.discard(match_id)
    await first.publish_shared(event(1, match_id))
    second, second_box = worker(match_id)
    # Simulate complete process loss; only the shared cache survives.
    first.compact.clear()
    await second.prepare_compact(match_id, PUBLIC_RESOURCES, event(0, match_id))
    restored = second.compact[match_id][PUBLIC_RESOURCES]
    frames = restored.replay(cursor)
    assert frames is not None
    assert len(frames) == 1
    result = decode(decoder, frames[0])
    assert result is not None
    assert result["revision"] == event(1, match_id)["revision"]
    second_box.discard(match_id)
    await second.publish_shared(event(2, match_id))
    result = decode(decoder, second_box.pending[match_id]["_wire"])
    assert result is not None
    assert (
        result["public_reads"]
        == second.compact[match_id][PUBLIC_RESOURCES].document["public_reads"]
    )


@pytest.mark.asyncio
async def test_active_workers_share_frames_and_deduplicate_late_deliveries() -> None:
    """Serving workers receive identical encoded bytes, even when one runs behind."""
    match_id = str(uuid4())
    first, first_box = worker(match_id)
    second, second_box = worker(match_id)
    for current in (first, second):
        await current.prepare_compact(match_id, PUBLIC_RESOURCES, event(0, match_id))
    first_box.discard(match_id)
    second_box.discard(match_id)
    await first.publish_shared(event(1, match_id))
    await second.publish_shared(event(1, match_id))
    assert first_box.pending[match_id]["_wire"] == second_box.pending[match_id]["_wire"]
    second_box.discard(match_id)
    await second.publish_shared(event(1, match_id))
    assert not second_box.pending
    await first.publish_shared(event(2, match_id))
    await second.publish_shared(event(2, match_id))
    assert (
        first.compact[match_id][PUBLIC_RESOURCES].sequence
        == second.compact[match_id][PUBLIC_RESOURCES].sequence
    )


@pytest.mark.asyncio
async def test_cache_outage_detaches_epoch_and_recovers_with_authoritative_reset() -> (
    None
):
    """An unavailable cache must never let workers fork the same sequence space."""
    match_id = str(uuid4())
    current, box = worker(match_id)
    await current.prepare_compact(match_id, PUBLIC_RESOURCES, event(0, match_id))
    codec = current.compact[match_id][PUBLIC_RESOURCES]
    decoder = CompactDecoder(match_id)
    decode(decoder, codec.snapshot()["_wire"])
    epoch = codec.epoch
    box.discard(match_id)
    with patch(
        "apps.game_tracker.composition.shared_compact_store.advance",
        side_effect=SharedCompactUnavailableError,
    ):
        await current.publish_shared(event(1, match_id))
    assert codec.epoch != epoch
    result = decode(decoder, box.pending[match_id]["_wire"])
    assert result is not None
    box.discard(match_id)
    await current.publish_shared(event(2, match_id))
    result = decode(decoder, box.pending[match_id]["_wire"])
    assert result is not None
    assert current.compact[match_id][PUBLIC_RESOURCES].epoch == epoch


@pytest.mark.asyncio
async def test_connection_wave_shares_even_incomplete_bootstrap_reads() -> None:
    """Missing public caches do not make every reconnect issue a Redis operation."""
    match_id = str(uuid4())
    current, _ = worker(match_id)
    initial = {"match_id": match_id, "revision": 1, "resources": [], "snapshot": True}
    store = SharedCompactStore()
    with patch(
        "apps.game_tracker.composition.shared_compact_store.advance",
        wraps=store.advance,
    ) as advance:
        await asyncio.gather(
            *(
                current.prepare_compact(match_id, PUBLIC_RESOURCES, initial)
                for _ in range(100)
            )
        )
    assert advance.call_count == 1


@pytest.mark.asyncio
async def test_warmed_bootstrap_during_throttle_cannot_fork_shared_sequence() -> None:
    """Late cache fills must not consume the next real publication's sequence."""
    match_id = str(uuid4())
    current, box = worker(match_id)
    initial = event(0, match_id)
    cold = {
        key: value
        for key, value in initial.items()
        if key not in {"public_reads", "live"}
    }
    decoder = CompactDecoder(match_id)
    with patch(
        "apps.game_tracker.adapters.outbound.match_fanout.monotonic", return_value=0
    ):
        await current.prepare_compact(match_id, PUBLIC_RESOURCES, cold)
        decode(decoder, current.seed_compact(match_id, PUBLIC_RESOURCES, cold)["_wire"])
        # The cache warms before the preparation throttle expires. The synchronous
        # handshake must use the canonical snapshot, without assigning local slots.
        await current.prepare_compact(match_id, PUBLIC_RESOURCES, initial)
        decode(
            decoder,
            current.seed_compact(match_id, PUBLIC_RESOURCES, initial)["_wire"],
        )
    box.discard(match_id)
    await current.publish_shared(event(1, match_id))
    assert match_id in box.pending
    result = decode(decoder, box.pending[match_id]["_wire"])
    assert result is not None
    assert result["revision"] == event(1, match_id)["revision"]

    # Fill missing resources canonically after the throttle, then reconnect on a
    # replacement worker and prove that subsequent patches use the same dictionary.
    warmed = {**initial, "revision": result["revision"]}
    await current.prepare_compact(match_id, PUBLIC_RESOURCES, warmed)
    if match_id in box.pending:
        decode(decoder, box.pending[match_id]["_wire"])
    codec = current.compact[match_id][PUBLIC_RESOURCES]
    cursor = f"{codec.epoch}:{codec.sequence}".encode()
    replacement, replacement_box = worker(match_id)
    await replacement.prepare_compact(match_id, PUBLIC_RESOURCES, warmed)
    restored = replacement.compact[match_id][PUBLIC_RESOURCES]
    assert restored.replay(cursor) == []
    assert restored.document == codec.document
    replacement_box.discard(match_id)
    await replacement.publish_shared(event(2, match_id))
    result = decode(decoder, replacement_box.pending[match_id]["_wire"])
    assert result is not None
    assert result["revision"] == event(2, match_id)["revision"]


@pytest.mark.parametrize("real_redis", [False, True])
def test_atomic_duplicate_publication_and_same_revision_projection(
    real_redis: bool,
) -> None:
    """Atomic CAS survives concurrent encoders and preserves projection-only changes."""
    cache_settings = settings.CACHES
    if real_redis:
        url = os.environ.get("PUBLIC_LIVE_TEST_REDIS_URL")
        if not url:
            pytest.skip("Set PUBLIC_LIVE_TEST_REDIS_URL to an isolated Redis database")
        cache_settings = {
            **cache_settings,
            "public_live": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": url,
            },
        }
    match_id = str(uuid4())
    initial = event(0, match_id)
    initial["public_reads"].pop("stats")
    store = SharedCompactStore()
    with override_settings(CACHES=cache_settings):
        codec = store.advance(match_id, PUBLIC_RESOURCES, initial, seed=True)
        cursor = f"{codec.epoch}:{codec.sequence}".encode()
        decoder = CompactDecoder(match_id)
        decode(decoder, codec.snapshot()["_wire"])

        def publish(_: int) -> CompactMatch:
            return store.advance(
                match_id, PUBLIC_RESOURCES, event(1, match_id), seed=False
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(publish, range(16)))
        assert len({(result.epoch, result.sequence) for result in results}) == 1
        latest = results[0]
        assert latest.sequence == codec.sequence + 1
        frames = latest.replay(cursor)
        assert frames is not None
        assert decode(decoder, frames[0]) is not None
        projection = {
            **event(1, match_id),
            "resources": ["stats"],
            "public_reads": {"stats": {"attempts": 987}},
        }
        latest = store.advance(match_id, PUBLIC_RESOURCES, projection, seed=False)
        duplicate = store.advance(
            match_id, PUBLIC_RESOURCES, event(1, match_id), seed=False
        )
        assert duplicate.sequence == latest.sequence
        assert (
            duplicate.document["public_reads"]["stats"]["attempts"]
            == projection["public_reads"]["stats"]["attempts"]
        )
        # Force two distinct projection writers to read the same Redis version.
        # One must retry its changes against the other's successful publication.
        if real_redis:
            barrier, attempt = Barrier(2), local()
            advance = SharedCompactStore._advance

            def racing_advance(
                old: bytes | None,
                identity: str,
                resources: frozenset[str],
                incoming: dict,
                *,
                seed: bool,
            ) -> tuple[CompactMatch, bytes | None]:
                if not getattr(attempt, "started", False):
                    attempt.started = True
                    barrier.wait(timeout=5)
                return advance(old, identity, resources, incoming, seed=seed)

            updates = [
                {
                    **projection,
                    "resources": [resource],
                    "public_reads": {resource: value},
                }
                for resource, value in [
                    ("stats", {"attempts": 456}),
                    ("summary", {"home_score": 789}),
                ]
            ]
            with (
                patch.object(
                    SharedCompactStore, "_advance", side_effect=racing_advance
                ),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                futures = [
                    pool.submit(
                        store.advance, match_id, PUBLIC_RESOURCES, update, seed=False
                    )
                    for update in updates
                ]
                for future in futures:
                    future.result()
            latest = store.advance(match_id, PUBLIC_RESOURCES, updates[0], seed=False)
            for update in updates:
                for resource, value in update["public_reads"].items():
                    assert latest.document["public_reads"][resource] == value
        # Missing state selects a fresh epoch rather than an unsafe replay.
        caches["public_live"].delete(
            f"compact-shared:v1:{match_id}:{','.join(sorted(PUBLIC_RESOURCES))}"
        )
        replacement = store.advance(
            match_id, PUBLIC_RESOURCES, event(2, match_id), seed=True
        )
        assert replacement.epoch != latest.epoch
        assert replacement.replay(cursor) is None


def test_shared_budgets_preserve_last_valid_checkpoint() -> None:
    """Oversized state and saturated deduplication retain the last valid checkpoint."""
    match_id = str(uuid4())
    store = SharedCompactStore()
    initial = store.advance(match_id, PUBLIC_RESOURCES, event(0, match_id), seed=True)
    with (
        patch("apps.game_tracker.adapters.outbound.shared_compact.MAX_STATE_BYTES", 1),
        pytest.raises(SharedCompactUnavailableError),
    ):
        store.advance(match_id, PUBLIC_RESOURCES, event(1, match_id), seed=False)
    restored = store.advance(match_id, PUBLIC_RESOURCES, event(0, match_id), seed=True)
    assert (restored.epoch, restored.sequence) == (initial.epoch, initial.sequence)
    store.advance(match_id, PUBLIC_RESOURCES, event(1, match_id), seed=False)
    projection = {
        **event(1, match_id),
        "resources": ["stats"],
        "public_reads": {"stats": {"attempts": 999}},
    }
    with patch("apps.game_tracker.adapters.outbound.shared_compact.MAX_SEEN", 1):
        with pytest.raises(SharedCompactUnavailableError):
            store.advance(match_id, PUBLIC_RESOURCES, projection, seed=False)
        # Known duplicate remains safe even when the bounded identity set is full.
        duplicate = store.advance(
            match_id, PUBLIC_RESOURCES, event(1, match_id), seed=False
        )
        assert duplicate.sequence == initial.sequence + 1
        newer = store.advance(
            match_id, PUBLIC_RESOURCES, event(2, match_id), seed=False
        )
        assert newer.revision == event(2, match_id)["revision"]
