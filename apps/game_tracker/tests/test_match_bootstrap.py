"""Public SSE starting snapshots share work and preserve resource boundaries."""

import asyncio
from time import time
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.adapters.outbound.match_bootstrap import (
    BootstrapCache,
    read_bootstrap,
)
from apps.game_tracker.composition import (
    prepare_public_match_reads,
    published_live_store,
    published_match_store,
)
from apps.game_tracker.services.public_live import publish_public_live
from apps.game_tracker.services.public_match_reads import resource_key
from apps.schedule.tests.match_api_test_support import create_match_graph


@pytest.mark.django_db(transaction=True)
def test_bootstrap_is_sql_free_full_and_revision_fenced() -> None:
    """A new viewer gets complete public bases without private tracker data."""
    graph = create_match_graph(prefix="Bootstrap")
    match_id = str(graph.match.pk)
    prepare_public_match_reads(match_id=match_id)
    publish_public_live(match_id=match_id, store=published_live_store)
    with CaptureQueriesContext(connection) as queries:
        snapshot = read_bootstrap(match_id, graph.match_data.live_revision)
    assert not queries
    assert snapshot is not None
    assert set(snapshot.event["public_reads"]) == {
        "summary",
        "stats",
        "events",
        "shots",
    }
    assert "events" in snapshot.event["public_reads"]["events"]
    assert "shots" in snapshot.event["public_reads"]["shots"]
    live = snapshot.live_state()
    assert live is not None
    assert live["match_id"] == match_id
    assert "live" not in snapshot.event
    assert read_bootstrap(match_id, graph.match_data.live_revision + 1) is None
    revision = graph.match_data.live_revision + 1
    for resource in ("summary", "stats", "events", "shots"):
        published_match_store.invalidate(resource_key(match_id, resource), revision)
    published_live_store.invalidate(match_id, revision)
    assert read_bootstrap(match_id, graph.match_data.live_revision) is None


@pytest.mark.asyncio
async def test_bootstrap_coalesces_many_connections_and_caches_misses() -> None:
    """A connection wave shares one cache read, including when Redis has no state."""
    cache = BootstrapCache()
    with patch(
        "apps.game_tracker.adapters.outbound.match_bootstrap.read_bootstrap",
        return_value=None,
    ) as read:
        results = await asyncio.gather(*(cache.get("match", 1) for _ in range(1000)))
        assert results == [None] * 1000
        read.assert_called_once_with("match", 1, budget=256 * 1024)
        await cache.get("match", 1)
        read.assert_called_once()
        await cache.get("match", 2)
        assert read.call_args_list[-1].args == ("match", 2)
    await cache.close()
    assert not cache.entries
    assert not cache.tasks


def test_bootstrap_omits_oversized_resources_without_dropping_smaller_ones() -> None:
    """Large timelines fall back independently, keeping connection frames bounded."""
    envelope = {
        "revision": 1,
        "created_at": time(),
        "payload": {"text": "x" * (256 * 1024)},
    }
    small = {**envelope, "payload": {"events": []}}
    with (
        patch.object(
            published_match_store, "get", side_effect=[envelope, small, None, None]
        ),
        patch.object(published_live_store, "get", return_value=None),
    ):
        snapshot = read_bootstrap("match", 1)
    assert snapshot is not None
    assert len(snapshot.event["public_reads"]) == 1
    maximum_frame = 1024
    assert len(snapshot.wire) < maximum_frame
