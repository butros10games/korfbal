"""Reuse native Redis clients across short-lived Django ASGI request threads."""

from __future__ import annotations

from os import getpid, register_at_fork
from secrets import randbelow
from threading import RLock
from typing import Any, ClassVar

from django.core.cache.backends.redis import RedisCache, RedisCacheClient
from django.utils.functional import cached_property
from django_prometheus.cache.backends.redis import NativeRedisCache
from redis import ConnectionPool, Redis


class _Registry:
    clients: ClassVar[
        list[tuple[int, tuple[str, ...], dict[str, Any], RedisCacheClient]]
    ] = []
    lock: ClassVar[RLock] = RLock()

    @classmethod
    def after_fork(cls) -> None:
        cls.lock = RLock()
        cls.clients = []


register_at_fork(after_in_child=_Registry.after_fork)


class _SharedClient(RedisCacheClient):
    _client: type[Redis]
    _pool_class: type[ConnectionPool]
    _pool_options: dict[str, Any]

    def __init__(self, servers: tuple[str, ...], options: dict[str, Any]) -> None:
        super().__init__(list(servers), **options)
        # Initialize under the registry lock; subsequent calls only choose a client.
        self._shared_clients = [
            self._client(
                connection_pool=self._pool_class.from_url(server, **self._pool_options)
            )
            for server in servers
        ]

    def get_client(self, key: object = None, *, write: bool = False) -> Redis:
        index = (
            0
            if write or len(self._shared_clients) == 1
            else 1 + randbelow(len(self._shared_clients) - 1)
        )
        return self._shared_clients[index]


class SharedRedisCache(RedisCache):
    """Keep pooled clients process-wide while retaining per-backend key policies."""

    _servers: list[str]
    _options: dict[str, Any]

    @cached_property
    def _cache(self) -> RedisCacheClient:
        servers = tuple(self._servers)
        pid = getpid()
        with _Registry.lock:
            for process, locations, options, client in _Registry.clients:
                if process == pid and locations == servers and options == self._options:
                    return client
            client = _SharedClient(servers, self._options)
            # Bound retained configurations, including test settings overrides.
            _Registry.clients.append((pid, servers, dict(self._options), client))
            del _Registry.clients[:-16]
            return client


class PrometheusRedisCache(NativeRedisCache, SharedRedisCache):
    """Retain native-cache metrics with the same shared connection lifecycle."""
