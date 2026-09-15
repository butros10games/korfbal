"""Shared, bounded public snapshot caching through Django's cache backend."""

from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from time import monotonic, sleep
from typing import Any

from django.core.cache import caches
from redis.exceptions import RedisError


class DjangoPublicLiveSnapshotCache:
    """Coalesce concurrent misses briefly; cache availability is optional."""

    def get_or_build(
        self, key: str, build: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """Reuse a revision for five minutes, with at most 100ms miss waiting."""
        cache = caches["public_live"]
        try:
            payload = cache.get(key)
            if payload is not None:
                return deepcopy(payload)
            owner = cache.add(f"{key}:building", True, timeout=2)
            if not owner:
                deadline = monotonic() + 0.1
                while monotonic() < deadline:
                    sleep(0.01)
                    payload = cache.get(key)
                    if payload is not None:
                        return deepcopy(payload)
        except (RedisError, OSError):
            return build()

        payload = build()
        with suppress(RedisError, OSError):
            cache.set(key, payload, timeout=300)
        # The short lease expires itself: deleting it could remove a newer lease
        # when a slow builder completes after its own lease has expired.
        return deepcopy(payload)
