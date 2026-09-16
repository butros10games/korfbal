"""Coalesce bounded public SSE bootstrap reads and encoded frames per match."""

import asyncio
from dataclasses import dataclass
import json
from time import monotonic, time
from typing import Any

from apps.game_tracker.adapters.outbound.published_live_store import MAX_SNAPSHOT_AGE
from apps.game_tracker.application.ports import PublicLiveStoreError
from apps.game_tracker.composition import published_live_store, published_match_store
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES
from apps.game_tracker.services.public_live import render_published_live
from apps.game_tracker.services.public_match_reads import (
    PUBLIC_MATCH_RESOURCES,
    resource_key,
)


@dataclass
class Bootstrap:
    """Share public bytes, rendering the small live clock at response time."""

    event: dict[str, Any]
    wire: bytes
    live: dict[str, Any] | None
    expires_at: float

    def live_state(self) -> dict[str, Any] | None:
        """Avoid reusing an old timer server_time for later connections."""
        return render_published_live(self.live) if self.live else None


def read_bootstrap(
    match_id: str, revision: int, *, budget: int = 256 * 1024
) -> Bootstrap | None:
    """Read committed public caches only; cold/outage recovery remains bounded HTTP."""
    remaining = budget
    payloads = {}
    expiry = time() + MAX_SNAPSHOT_AGE
    for resource in PUBLIC_MATCH_RESOURCES:
        envelope = published_match_store.get(resource_key(match_id, resource))
        if envelope is None or envelope["revision"] != revision:
            continue
        payload = envelope["payload"]
        size = len(json.dumps(payload, separators=(",", ":")).encode())
        if size <= remaining:
            expiry = min(expiry, envelope["created_at"] + MAX_SNAPSHOT_AGE)
            payloads[resource] = payload
            remaining -= size
    live = published_live_store.get(match_id)
    if live is not None and live["revision"] != revision:
        live = None
    if live is not None:
        expiry = min(expiry, live["created_at"] + MAX_SNAPSHOT_AGE)
    if not payloads and live is None:
        return None
    event = {
        "match_id": match_id,
        "revision": revision,
        "snapshot": True,
        "resources": sorted(ALL_LIVE_RESOURCES),
        "public_reads": payloads,
    }
    data = json.dumps(event, separators=(",", ":"))
    return Bootstrap(
        event,
        f"event: match.changed\ndata: {data}\n\n".encode(),
        live,
        monotonic() + max(0, expiry - time()),
    )


class BootstrapCache:
    """One bounded cache read task per local match; no work per spectator."""

    def __init__(self, *, budget: int = 256 * 1024) -> None:
        """Scope all cached frames and tasks to the owning fanout lifecycle."""
        self.budget = budget
        self.entries: dict[str, tuple[int, float, Bootstrap | None]] = {}
        self.tasks: dict[tuple[str, int], asyncio.Task[Bootstrap | None]] = {}

    async def get(self, match_id: str, revision: int) -> Bootstrap | None:
        """Reuse a short-lived frame or join its in-flight refresh."""
        entry = self.entries.get(match_id)
        if entry and entry[0] == revision and monotonic() < entry[1]:
            return entry[2]
        key = (match_id, revision)
        task = self.tasks.get(key)
        if task is None:
            task = self.tasks[key] = asyncio.create_task(self._load(match_id, revision))
        return await asyncio.shield(task)

    async def _load(self, match_id: str, revision: int) -> Bootstrap | None:
        try:
            result = await asyncio.to_thread(
                read_bootstrap, match_id, revision, budget=self.budget
            )
            # A revision can be visible shortly before its publication finishes.
            # Cache misses briefly too, so a reconnect wave cannot stampede Redis.
            self.entries[match_id] = (
                revision,
                min(monotonic() + 5, result.expires_at)
                if result
                else monotonic() + 0.25,
                result,
            )
            return result
        except PublicLiveStoreError:
            self.entries[match_id] = (revision, monotonic() + 0.25, None)
            return None
        finally:
            if self.tasks.get((match_id, revision)) is asyncio.current_task():
                self.tasks.pop((match_id, revision), None)

    def forget(self, match_id: str) -> None:
        """Release frames and unfinished reads after the last local viewer leaves."""
        self.entries.pop(match_id, None)
        for key in list(self.tasks):
            if key[0] == match_id:
                self.tasks.pop(key).cancel()

    async def close(self) -> None:
        """Stop only work owned by the closing fanout."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()
