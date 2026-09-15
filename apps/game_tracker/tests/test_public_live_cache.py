"""Cache misses remain bounded and cannot change public response ownership."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import socket
from threading import Event
from time import monotonic
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.cache import caches
from django.test import override_settings
from korfbal.settings import services
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from apps.game_tracker.adapters.outbound.public_live_cache import (
    DjangoPublicLiveSnapshotCache,
)


def test_concurrent_misses_share_one_build() -> None:
    """A second reader waits briefly for the process sharing its revision."""
    started, waiting, release = Event(), Event(), Event()
    backend = caches["public_live"]
    original_add = backend.add

    def add(key: str, value: object, timeout: float) -> bool:
        result = original_add(key, value, timeout=timeout)
        if not result:
            waiting.set()
        return result

    def build() -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2)
        return {"score": {"home": 1}}

    builder = Mock(side_effect=build)
    adapter = DjangoPublicLiveSnapshotCache()
    with (
        patch(
            "apps.game_tracker.adapters.outbound.public_live_cache.caches",
            {"public_live": backend},
        ),
        patch.object(backend, "add", side_effect=add),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(adapter.get_or_build, "concurrent-public", builder)
        try:
            assert started.wait(timeout=2)
            second = pool.submit(adapter.get_or_build, "concurrent-public", builder)
            assert waiting.wait(timeout=2)
        finally:
            release.set()
        assert first.result(timeout=2) == second.result(timeout=2)
    assert builder.call_count == 1


@pytest.mark.parametrize("operation", ["get", "add", "set"])
def test_cache_failure_falls_back_to_authoritative_builder(operation: str) -> None:
    """Redis failures never discard an otherwise successful database read."""
    builder = Mock(return_value={"live_revision": 7})
    with patch.object(
        caches["public_live"], operation, side_effect=RedisConnectionError("offline")
    ):
        assert DjangoPublicLiveSnapshotCache().get_or_build("offline", builder) == {
            "live_revision": 7
        }
    builder.assert_called_once_with()


def test_abandoned_builder_does_not_block_readers() -> None:
    """An expired wait budget allows a fresh read even while the lease remains."""
    caches["public_live"].add("abandoned:building", True, timeout=2)
    builder = Mock(return_value={"live_revision": 4})
    with patch(
        "apps.game_tracker.adapters.outbound.public_live_cache.monotonic",
        side_effect=[0, 1],
    ):
        assert DjangoPublicLiveSnapshotCache().get_or_build("abandoned", builder) == {
            "live_revision": 4
        }
    builder.assert_called_once_with()


def test_callers_cannot_mutate_cached_payloads() -> None:
    """Clock fields and polling resources stay local to each response."""
    adapter = DjangoPublicLiveSnapshotCache()
    first = adapter.get_or_build(
        "immutable-public", lambda: {"timer": {"type": "active"}}
    )
    first["timer"]["server_time"] = "caller-specific"
    first["resources"] = ["shots"]
    unexpected_build = Mock(side_effect=AssertionError("cache miss"))
    assert adapter.get_or_build("immutable-public", unexpected_build) == {
        "timer": {"type": "active"}
    }


@pytest.mark.parametrize("operation", ["get", "add", "set"])
def test_stalled_redis_falls_back_promptly(operation: str) -> None:
    """A silent Redis socket cannot retain a reader for seconds."""
    max_fallback_seconds = 0.75
    release = Event()
    accepted = Event()
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def stall() -> None:
            with listener.accept()[0] as peer:
                accepted.set()
                # Read the handshake, then deliberately send no response.
                peer.settimeout(2)
                peer.recv(4096)
                release.wait(timeout=2)

        server = pool.submit(stall)
        config = deepcopy(services.CACHES["public_live"])
        config["LOCATION"] = f"redis://127.0.0.1:{listener.getsockname()[1]}/1"
        with override_settings(CACHES={**settings.CACHES, "public_live": config}):
            backend = caches["public_live"]
            builder = Mock(return_value={"live_revision": 7})
            try:
                with ExitStack() as patches:
                    if operation in {"add", "set"}:
                        patches.enter_context(
                            patch.object(backend, "get", return_value=None)
                        )
                    if operation == "set":
                        patches.enter_context(
                            patch.object(backend, "add", return_value=True)
                        )
                    started = monotonic()
                    result = DjangoPublicLiveSnapshotCache().get_or_build(
                        "stalled", builder
                    )
                    elapsed = monotonic() - started
                assert result == {"live_revision": 7}
                builder.assert_called_once_with()
                assert accepted.is_set()
                # Allow scheduler headroom, but reject Redis's five-second default.
                assert elapsed < max_fallback_seconds
            finally:
                release.set()
                backend.close()
                server.result(timeout=3)
