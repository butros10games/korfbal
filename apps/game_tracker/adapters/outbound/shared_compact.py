"""Bounded canonical compact state shared by SSE workers, with atomic publication."""

from collections import deque
from hashlib import sha256
import json
from threading import RLock
from typing import Any
from uuid import uuid4

from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache
from redis.exceptions import RedisError

from .compact_match import CompactMatch


STATE_TTL = 120
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_SEEN = 64
_local_lock = RLock()
_CAS = """
local old = redis.call('GET', KEYS[1])
local token = old and string.sub(old, 1, 32) or ''
if token ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


class SharedCompactUnavailableError(Exception):
    """Use local encoding and authoritative resets when sharing is unavailable."""


def _decode(
    raw: bytes, match_id: str, resources: frozenset[str]
) -> tuple[CompactMatch, list[str]]:
    if len(raw) > MAX_STATE_BYTES:
        raise SharedCompactUnavailableError
    state = json.loads(raw[33:])
    codec = CompactMatch(match_id, resources)
    codec.epoch = state["epoch"]
    codec.sequence = state["sequence"]
    codec.revision = state["revision"]
    codec.document = state["document"]
    codec.dictionary.strings = state["strings"]
    codec.dictionary.index = {
        value: index for index, value in enumerate(state["strings"])
    }
    codec.history = deque((number, wire.encode()) for number, wire in state["history"])
    codec.history_bytes = sum(len(wire) for _, wire in codec.history)
    return codec, state["seen"]


def _encode(codec: CompactMatch, seen: list[str]) -> bytes:
    state = {
        "epoch": codec.epoch,
        "sequence": codec.sequence,
        "revision": codec.revision,
        "document": codec.document,
        "strings": codec.dictionary.strings,
        "history": [(number, wire.decode()) for number, wire in codec.history],
        "seen": seen,
    }
    raw = (uuid4().hex + "\n" + json.dumps(state, separators=(",", ":"))).encode()
    if len(raw) > MAX_STATE_BYTES:
        raise SharedCompactUnavailableError
    return raw


class SharedCompactStore:
    """Compare-and-swap one public encoder per match/resource mask across workers."""

    def advance(
        self,
        match_id: str,
        resources: frozenset[str],
        event: dict[str, Any],
        *,
        seed: bool,
    ) -> CompactMatch:
        """Return canonical state; duplicate deliveries share one stream sequence.

        Raises:
            SharedCompactUnavailableError: Sharing requires snapshot fallback.

        """
        backend = caches["public_live"]
        key = f"compact-shared:v1:{match_id}:{','.join(sorted(resources))}"
        try:
            if isinstance(backend, LocMemCache):
                with _local_lock:
                    old = backend.get(key)
                    codec, raw = self._advance(
                        old, match_id, resources, event, seed=seed
                    )
                    if raw is not None:
                        backend.set(key, raw, timeout=STATE_TTL)
                    return codec
            if not isinstance(backend, RedisCache):
                raise SharedCompactUnavailableError
            client = backend._cache.get_client(write=True)
            key = backend.make_key(key)
            for _ in range(3):
                old = client.get(key)
                codec, raw = self._advance(old, match_id, resources, event, seed=seed)
                if raw is None or client.eval(
                    _CAS, 1, key, old[:32] if old else b"", raw, STATE_TTL
                ):
                    return codec
        except (RedisError, OSError, ValueError, KeyError, TypeError) as exc:
            raise SharedCompactUnavailableError from exc
        raise SharedCompactUnavailableError

    @staticmethod
    def _advance(
        old: bytes | None,
        match_id: str,
        resources: frozenset[str],
        event: dict[str, Any],
        *,
        seed: bool,
    ) -> tuple[CompactMatch, bytes | None]:
        digest = sha256(
            json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        codec, seen = (
            _decode(old, match_id, resources)
            if old
            else (CompactMatch(match_id, resources), [])
        )
        if event["revision"] < codec.revision or (not seed and digest in seen):
            return codec, None
        if event["revision"] > codec.revision:
            seen = []
        before = (codec.epoch, codec.sequence)
        if seed:
            codec.seed(event)
        else:
            # Never evict same-revision identities and allow a delayed duplicate
            # to roll back a newer projection. Saturation recovers locally.
            if len(seen) >= MAX_SEEN:
                raise SharedCompactUnavailableError
            codec.publish(event)
            seen.append(digest)
        if old is not None and before == (codec.epoch, codec.sequence):
            return codec, None
        return codec, _encode(codec, seen)
