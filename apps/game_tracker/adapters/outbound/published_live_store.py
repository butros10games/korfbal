"""Atomic latest-snapshot publication on the dedicated bounded Redis connection."""

from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from functools import wraps
import json
from threading import RLock
from time import monotonic, sleep, time
from typing import Any

from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache
from redis.exceptions import RedisError

from apps.game_tracker.application.ports import PublicLiveStoreError


MAX_SNAPSHOT_AGE = 30
_FENCE_TTL = 60
_local_lock = RLock()
# Compare and replace in one Redis operation: delayed workers cannot regress state.
_PUBLISH = """
local old = redis.call('GET', KEYS[1])
local incoming = cjson.decode(ARGV[1])
if old then
    local current = cjson.decode(old)
    if current.revision > incoming.revision then return 0 end
    if current.revision == incoming.revision then
        if ARGV[3] == 'fence' then return 0 end
        if current.created_at > incoming.created_at then return 0 end
    end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


def _storage_errors[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except (RedisError, OSError) as exc:
            raise PublicLiveStoreError("Public snapshot storage unavailable") from exc

    return guarded


class SharedPublishedLiveStore:
    """Share public envelopes; retain short-lived fences even without a payload."""

    def __init__(self, namespace: str = "public-live:published:v1") -> None:
        """Isolate resource envelopes while sharing atomic publication semantics."""
        self.namespace = namespace

    def _key(self, match_id: str) -> str:
        return f"{self.namespace}:{match_id}"

    @_storage_errors
    def get(self, match_id: str) -> dict[str, Any] | None:
        """Return fresh state; callers recover from cache errors or expired state."""
        backend = caches["public_live"]
        key = self._key(match_id)
        if isinstance(backend, RedisCache):
            raw = backend._cache.get_client(write=True).get(backend.make_key(key))
            envelope = json.loads(raw) if raw is not None else None
        else:
            envelope = backend.get(key)
        if (
            envelope is None
            or envelope["payload"] is None
            or not 0 <= time() - envelope["created_at"] < MAX_SNAPSHOT_AGE
        ):
            return None
        return deepcopy(envelope)

    @_storage_errors
    def _replace(self, match_id: str, envelope: dict[str, Any], *, fence: bool) -> None:
        backend = caches["public_live"]
        key = self._key(match_id)
        if isinstance(backend, RedisCache):
            backend._cache.get_client(write=True).eval(
                _PUBLISH,
                1,
                backend.make_key(key),
                json.dumps(envelope),
                _FENCE_TTL,
                "fence" if fence else "snapshot",
            )
            return
        if not isinstance(backend, LocMemCache):
            raise TypeError(
                "Published live state requires Redis or local-memory cache."
            )
        # LocMem is for isolated tests only; production comparison is atomic in Redis.
        with _local_lock:
            current = backend.get(key)
            if current is not None and (
                current["revision"] > envelope["revision"]
                or (
                    current["revision"] == envelope["revision"]
                    and (fence or current["created_at"] > envelope["created_at"])
                )
            ):
                return
            backend.set(key, envelope, timeout=_FENCE_TTL)

    def put(self, match_id: str, envelope: dict[str, Any]) -> None:
        """Publish a consistent envelope without overwriting a newer revision."""
        self._replace(match_id, envelope, fence=False)

    def invalidate(self, match_id: str, revision: int) -> None:
        """Reject snapshots older than this committed revision."""
        self._replace(
            match_id,
            {"revision": revision, "created_at": 0, "payload": None},
            fence=True,
        )

    def recover(
        self,
        match_id: str,
        minimum_revision: int,
        build: Callable[[], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Bound recovery fan-in without waiting while holding database connections."""
        try:
            owner = caches["public_live"].add(
                f"{self._key(match_id)}:building", True, timeout=2
            )
            if not owner:
                deadline = monotonic() + 0.1
                while monotonic() < deadline:
                    value = self.get(match_id)
                    if value is not None and value["revision"] >= minimum_revision:
                        return value
                    sleep(0.01)
        except (RedisError, OSError, PublicLiveStoreError):
            return build()
        value = build()
        if value is not None:
            with suppress(PublicLiveStoreError):
                self.put(match_id, value)
        # Expire the lease; deleting it could remove a successor's lease.
        return value
