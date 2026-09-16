"""Request-thread cache lifecycle and native publication compatibility."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from django.core.cache.backends.redis import RedisCache
import pytest
from redis.backoff import NoBackoff
from redis.retry import Retry

from apps.kwt_common.adapters.outbound.redis_cache import (
    PrometheusRedisCache,
    SharedRedisCache,
)


def test_request_threads_reuse_client_but_preserve_key_namespaces() -> None:
    """New backends must reuse a pool without sharing their key prefixes."""
    count = 32
    location = f"redis://localhost:6379/{uuid4().int % 100000}"

    def create(index: int) -> tuple[object, str]:
        backend = SharedRedisCache(location, {"KEY_PREFIX": str(index)})
        return backend._cache.get_client(write=True), backend.make_key("score")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(count)))
    assert all(client is results[0][0] for client, _ in results)
    assert len({key for _, key in results}) == count


def test_client_configuration_and_primary_replica_routes_stay_isolated() -> None:
    """Short public-cache timeouts must not leak into the session cache."""
    primary, replica = 10, 11
    locations = [
        f"redis://localhost:6379/{primary}",
        f"redis://localhost:6379/{replica}",
    ]
    ordinary = SharedRedisCache(locations, {})
    bounded = SharedRedisCache(
        locations,
        {"OPTIONS": {"socket_timeout": 0.05, "retry": Retry(NoBackoff(), 0)}},
    )
    writer = bounded._cache.get_client(write=True)
    reader = bounded._cache.get_client(write=False)
    assert writer.connection_pool.connection_kwargs["db"] == primary
    assert reader.connection_pool.connection_kwargs["db"] == replica
    assert writer.connection_pool.connection_kwargs["socket_timeout"] == pytest.approx(
        0.05
    )
    assert ordinary._cache.get_client(write=True) is not writer
    assert (
        ordinary._cache.get_client(write=True).connection_pool.connection_kwargs.get(
            "socket_timeout"
        )
        is None
    )


def test_prometheus_backend_supports_native_atomic_publication() -> None:
    """Metrics must not switch to the incompatible django-redis backend."""
    backend = PrometheusRedisCache("redis://localhost:6379/12", {})
    plain = SharedRedisCache("redis://localhost:6379/12", {})
    assert isinstance(backend, RedisCache)
    assert backend._cache.get_client(write=True) is plain._cache.get_client(write=True)
