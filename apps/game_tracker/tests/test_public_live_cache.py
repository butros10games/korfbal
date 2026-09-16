"""Atomic shared publication and bounded failure recovery contracts."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
import os
import socket
from threading import Event
from time import monotonic, time
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.test import override_settings
from korfbal.settings import services
import pytest

from apps.game_tracker.adapters.outbound.published_live_store import (
    SharedPublishedLiveStore,
)
from apps.game_tracker.application.ports import PublicLiveStoreError


@pytest.fixture(params=["locmem", "redis"])
def store(request: pytest.FixtureRequest) -> Iterator[SharedPublishedLiveStore]:
    """Exercise both cache backends.

    Yields:
        Store using local memory or the explicitly configured isolated Redis.

    """
    config = deepcopy(settings.CACHES["public_live"])
    if request.param == "redis":
        url = os.environ.get("PUBLIC_LIVE_TEST_REDIS_URL")
        if not url:
            pytest.skip("Set PUBLIC_LIVE_TEST_REDIS_URL to an isolated Redis database")
        config = {**services.CACHES["public_live"], "LOCATION": url}
    with override_settings(CACHES={**settings.CACHES, "public_live": config}):
        yield SharedPublishedLiveStore()
        caches["public_live"].close()


def envelope(revision: int) -> dict[str, Any]:
    """Synthetic public snapshot without identifiers or private player state."""
    return {
        "revision": revision,
        "created_at": time(),
        "payload": {"live_revision": revision, "score": {"home": revision}},
        "history": [],
    }


def test_out_of_order_publication_cannot_regress(
    store: SharedPublishedLiveStore,
) -> None:
    """A delayed worker cannot replace a newer completed snapshot."""
    match_id = str(uuid4())
    newest = 9
    store.put(match_id, envelope(newest))
    store.put(match_id, envelope(8))
    result = store.get(match_id)
    assert result is not None
    assert result["revision"] == newest


def test_fence_rejects_old_workers_but_accepts_current_revision(
    store: SharedPublishedLiveStore,
) -> None:
    """Committed writes invalidate earlier snapshots without delaying fresh workers."""
    match_id = str(uuid4())
    store.put(match_id, envelope(1))
    newest = 2
    store.invalidate(match_id, newest)
    store.put(match_id, envelope(1))
    assert store.get(match_id) is None
    store.put(match_id, envelope(2))
    store.invalidate(match_id, 2)
    store.invalidate(match_id, 1)
    result = store.get(match_id)
    assert result is not None
    assert result["revision"] == newest


def test_expired_or_slow_snapshot_requires_recovery(
    store: SharedPublishedLiveStore,
) -> None:
    """Late completion cannot extend a snapshot's freshness window."""
    match_id = str(uuid4())
    value = envelope(1)
    store.put(match_id, value)
    with patch(
        "apps.game_tracker.adapters.outbound.published_live_store.time",
        return_value=value["created_at"] + 31,
    ):
        assert store.get(match_id) is None
        store.put(match_id, value)
        assert store.get(match_id) is None


def test_deletion_fence_and_caller_isolation(store: SharedPublishedLiveStore) -> None:
    """Deleted match state cannot be resurrected by an in-flight reader."""
    match_id = str(uuid4())
    value = envelope(1)
    store.put(match_id, value)
    result = store.get(match_id)
    assert result is not None
    result["payload"]["score"]["home"] = 999
    saved = store.get(match_id)
    assert saved is not None
    assert saved["payload"]["score"]["home"] == 1
    store.invalidate(match_id, 2**53 - 1)
    store.put(match_id, value)
    assert store.get(match_id) is None


def test_concurrent_recovery_shares_one_build(store: SharedPublishedLiveStore) -> None:
    """Readers waiting on a cold snapshot reuse the first completed build."""
    started, release = Event(), Event()
    match_id = str(uuid4())

    def build() -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=2)
        return envelope(1)

    builder = Mock(side_effect=build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.recover, match_id, 0, builder)
        assert started.wait(timeout=2)
        second = pool.submit(store.recover, match_id, 0, builder)
        release.set()
        assert first.result(timeout=2) == second.result(timeout=2)
    builder.assert_called_once_with()


@pytest.mark.parametrize("operation", ["get", "put"])
def test_stalled_redis_uses_bounded_socket_timeout(operation: str) -> None:
    """An unresponsive Redis server cannot hold a public request for seconds."""
    release = Event()
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def stall() -> None:
            with listener.accept()[0] as peer:
                peer.settimeout(2)
                peer.recv(4096)
                release.wait(timeout=2)

        server = pool.submit(stall)
        config = {
            **services.CACHES["public_live"],
            "LOCATION": f"redis://127.0.0.1:{listener.getsockname()[1]}/1",
        }
        with override_settings(CACHES={**settings.CACHES, "public_live": config}):
            adapter = SharedPublishedLiveStore()
            started = monotonic()
            try:
                call = (
                    partial(adapter.get, "stalled")
                    if operation == "get"
                    else partial(adapter.put, "stalled", envelope(1))
                )
                with pytest.raises(PublicLiveStoreError):
                    call()
                maximum_seconds = 0.75
                assert monotonic() - started < maximum_seconds
            finally:
                release.set()
                caches["public_live"].close()
                server.result(timeout=3)
