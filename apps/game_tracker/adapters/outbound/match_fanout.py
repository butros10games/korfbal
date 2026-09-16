"""One broker subscription per serving event loop with bounded viewer mailboxes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from time import monotonic
from typing import Any

from channels.layers import get_channel_layer
from django.conf import settings

from apps.game_tracker.composition import shared_compact_store
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES
from apps.game_tracker.realtime.metrics import SSE_SHARED_COMPACT
from apps.game_tracker.realtime.publisher import match_group_name

from .compact_match import CompactMatch
from .match_bootstrap import BootstrapCache
from .shared_compact import SharedCompactUnavailableError


MAX_COALESCED_VARIANTS = 32
logger = logging.getLogger(__name__)
_workers: dict[asyncio.AbstractEventLoop, Fanout] = {}
RevisionReader = Callable[[tuple[str, ...]], Awaitable[dict[str, int]]]


def encode_event(event: dict[str, Any]) -> bytes:
    """Serialize public fields once for all viewers of the same update."""
    payload = {
        key: event[key]
        for key in (
            "match_id",
            "revision",
            "resources",
            "live",
            "public_reads",
            "published_at",
            "snapshot",
        )
        if key in event
    }
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: match.changed\ndata: {data}\n\n".encode()


class Mailbox:
    """Retain at most one pending revision per subscribed match."""

    def __init__(
        self, match_ids: tuple[str, ...], *, resources: frozenset[str] | None = None
    ) -> None:
        """Initialize an empty bounded mailbox."""
        self.match_ids = match_ids
        self.resources = resources
        self.pending: dict[str, dict[str, Any]] = {}
        self.changed = asyncio.Event()

    def put(
        self, event: dict[str, Any], *, coalesced: dict[str, Any] | None = None
    ) -> None:
        """Merge invalidations while replacing obsolete score snapshots."""
        match_id = event["match_id"]
        previous = self.pending.get(match_id)
        if event.get("_compact"):
            pass
        elif previous is not None and coalesced is not None:
            event = coalesced
        elif previous is not None:
            newest = event if event["revision"] >= previous["revision"] else previous
            event = {
                **newest,
                "resources": sorted(
                    set(previous["resources"]) | set(event["resources"])
                ),
            }
            event["_wire"] = encode_event(event)
        self.pending[match_id] = event
        self.changed.set()

    def discard(self, match_id: str) -> None:
        """Remove delivery already covered by a synchronous reconnect replay."""
        self.pending.pop(match_id, None)
        if not self.pending:
            self.changed.clear()

    async def receive(self) -> dict[str, Any]:
        """Wait for the next match update without holding a broker connection."""
        await self.changed.wait()
        match_id = next(iter(self.pending))
        event = self.pending.pop(match_id)
        if not self.pending:
            self.changed.clear()
        return event


class Fanout:
    """Share channel reception and durable recovery between local viewers."""

    def __init__(self, read_revisions: RevisionReader) -> None:
        """Initialize one local broker receiver and recovery loop."""
        self.bootstraps = BootstrapCache()
        self.compact_bootstraps = BootstrapCache(budget=1024 * 1024)
        self.compact: dict[str, dict[frozenset[str], CompactMatch]] = {}
        self.layer = get_channel_layer()
        self.read_revisions = read_revisions
        self.compact_shared: set[tuple[str, frozenset[str]]] = set()
        self.compact_lock = asyncio.Lock()
        self.compact_prepared: dict[tuple[str, frozenset[str]], tuple[int, float]] = {}
        self.channel = ""
        self.lock = asyncio.Lock()
        self.subscribers: dict[str, set[Mailbox]] = {}
        self.latest: dict[str, int] = {}
        self.recovery_seen: dict[str, int] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.leases = 0
        self.initial_ids: set[str] = set()
        self.initial_task: asyncio.Task[dict[str, int]] | None = None
        self.initial_tasks: set[asyncio.Task[dict[str, int]]] = set()

    async def current_revisions(self, match_ids: tuple[str, ...]) -> dict[str, int]:
        """Batch concurrent connection handshakes instead of reading per viewer."""
        self.initial_ids.update(match_ids)
        if self.initial_task is None:
            self.initial_task = asyncio.create_task(self._initial_revisions())
            self.initial_tasks.add(self.initial_task)
            self.initial_task.add_done_callback(self.initial_tasks.discard)
        revisions = await asyncio.shield(self.initial_task)
        return {key: revisions[key] for key in match_ids if key in revisions}

    async def _initial_revisions(self) -> dict[str, int]:
        await asyncio.sleep(0.01)
        ids = tuple(self.initial_ids)
        self.initial_ids.clear()
        self.initial_task = None
        revisions = {}
        for offset in range(0, len(ids), 500):
            revisions.update(await self.read_revisions(ids[offset : offset + 500]))
        return revisions

    async def subscribe(self, mailbox: Mailbox) -> None:
        """Join each broker group before the caller reads its ready revision.

        Raises:
            RuntimeError: No channel layer is configured.

        """
        async with self.lock:
            if self.layer is None:
                raise RuntimeError("No Channels layer configured")
            if not self.channel:
                self.channel = await self.layer.new_channel()
                self.tasks = [
                    asyncio.create_task(self.run()),
                    asyncio.create_task(self.reconcile()),
                ]
            for match_id in mailbox.match_ids:
                viewers = self.subscribers.setdefault(match_id, set())
                viewers.add(mailbox)
                if mailbox.resources is not None:
                    cohorts = self.compact.setdefault(match_id, {})
                    if mailbox.resources not in cohorts:
                        cohorts[mailbox.resources] = CompactMatch(
                            match_id, mailbox.resources
                        )
                if len(viewers) == 1:
                    await self.layer.group_add(match_group_name(match_id), self.channel)

    async def prepare_compact(
        self, match_id: str, resources: frozenset[str], event: dict[str, Any]
    ) -> None:
        """Seed once per worker/cohort; simultaneous handshakes share the result."""
        async with self.compact_lock:
            codec = self.compact.get(match_id, {}).get(resources)
            if codec is None or (
                codec.revision >= event["revision"] and not codec.missing()
            ):
                return
            key = (match_id, resources)
            prepared = self.compact_prepared.get(key)
            if (
                prepared
                and prepared[0] == event["revision"]
                and monotonic() < prepared[1]
            ):
                return
            await self._shared_codec(match_id, resources, event, seed=True)
            self.compact_prepared[key] = (event["revision"], monotonic() + 0.25)

    async def _shared_codec(
        self,
        match_id: str,
        resources: frozenset[str],
        event: dict[str, Any],
        *,
        seed: bool,
    ) -> dict[str, Any] | None:
        old = self.compact.get(match_id, {}).get(resources)
        if old is None:
            return None
        try:
            codec = await asyncio.to_thread(
                shared_compact_store.advance, match_id, resources, event, seed=seed
            )
        except SharedCompactUnavailableError:
            SSE_SHARED_COMPACT.labels(result="fallback").inc()
            self._detach_compact(match_id, resources, old)
            return None
        SSE_SHARED_COMPACT.labels(result="success").inc()
        if self.compact.get(match_id, {}).get(resources) is not old:
            return None
        # A new epoch or a missed sequence needs one shared replacement snapshot.
        replay = codec.replay(f"{old.epoch}:{old.sequence}".encode())
        self.compact[match_id][resources] = codec
        self.compact_shared.add((match_id, resources))
        if replay is not None and len(replay) <= 1:
            frame = {
                "type": "match.changed",
                "match_id": match_id,
                "revision": codec.revision,
                "resources": event["resources"],
                "_compact": True,
                "_skip": not replay,
                "_wire": replay[0] if replay else b"",
            }
        else:
            frame = codec.snapshot()
        if seed and (old.epoch, old.sequence) != (codec.epoch, codec.sequence):
            for mailbox in self.subscribers.get(match_id, ()):
                if mailbox.resources == resources:
                    mailbox.put(codec.snapshot())
        return frame

    def _detach_compact(
        self, match_id: str, resources: frozenset[str], codec: CompactMatch
    ) -> None:
        key = (match_id, resources)
        if (
            key not in self.compact_shared
            or self.compact.get(match_id, {}).get(resources) is not codec
        ):
            return
        self.compact_shared.discard(key)
        # Cache failure must never fork the shared dictionary/sequence.
        codec.full = None
        frame = codec._reset(codec.document)
        for mailbox in self.subscribers.get(match_id, ()):
            if mailbox.resources == resources:
                mailbox.put(frame)

    async def publish_shared(self, event: dict[str, Any]) -> None:
        """Coordinate encoders per update, keeping Redis off viewer delivery loops."""
        async with self.compact_lock:
            frames = {}
            for resources in list(self.compact.get(event["match_id"], {})):
                frame = await self._shared_codec(
                    event["match_id"], resources, event, seed=False
                )
                if frame is not None:
                    frames[resources] = frame
            self.publish(event, frames=frames)

    def publish(
        self,
        event: dict[str, Any],
        *,
        frames: dict[frozenset[str], dict[str, Any]] | None = None,
    ) -> None:
        """Fan out a shared frame; slow viewers never block the broker receiver."""
        match_id = event["match_id"]
        if match_id not in self.subscribers or event["revision"] < self.latest.get(
            match_id, -1
        ):
            return
        self.latest[match_id] = event["revision"]
        self.recovery_seen.pop(match_id, None)
        legacy_event = None
        compact_frames = {
            mask: frames[mask]
            if frames is not None and mask in frames
            else codec.publish(event)
            for mask, codec in self.compact.get(match_id, {}).items()
        }
        replacements: dict[frozenset[str], dict[str, Any]] = {}
        for mailbox in self.subscribers.get(match_id, ()):
            if mailbox.resources is not None:
                frame = compact_frames[mailbox.resources]
                if frame.get("_skip"):
                    continue
                if match_id in mailbox.pending:
                    frame = self.compact[match_id][mailbox.resources].snapshot()
                mailbox.put(frame)
                continue
            if legacy_event is None:
                legacy_event = {**event, "_wire": encode_event(event)}
            coalesced = None
            if (previous := mailbox.pending.get(match_id)) is not None:
                resources = frozenset(previous["resources"]) | frozenset(
                    event["resources"]
                )
                # Share by invalidation set, not by viewer. Bound unusual mixes
                # without discarding a required invalidation.
                if (
                    resources not in replacements
                    and len(replacements) >= MAX_COALESCED_VARIANTS
                ):
                    resources = frozenset(ALL_LIVE_RESOURCES)
                coalesced = replacements.get(resources)
                if coalesced is None:
                    coalesced = {**event, "resources": sorted(resources)}
                    coalesced["_wire"] = encode_event(coalesced)
                    replacements[resources] = coalesced
            mailbox.put(legacy_event, coalesced=coalesced)

    async def run(self) -> None:
        """Receive broker updates with transient outage recovery."""
        assert self.layer is not None
        while True:
            try:
                event = await self.layer.receive(self.channel)
                if event["type"] == "match.changed":
                    await self.publish_shared(event)
            except Exception:
                logger.exception("Match fanout receive failed")
                await asyncio.sleep(1)

    async def reconcile(self) -> None:
        """Recover lost notifications with one bounded batch per local match set."""
        while True:
            await asyncio.sleep(settings.KORFBAL_SSE_RECONCILE_SECONDS)
            try:
                ids = tuple(self.subscribers)
                for offset in range(0, len(ids), 500):
                    revisions = await self.read_revisions(ids[offset : offset + 500])
                    for match_id, revision in revisions.items():
                        await self._recover(match_id, revision)
                        await self._recover_compact(match_id, revision)
                # Channels groups expire even while a channel remains connected.
                if self.layer is not None:
                    for match_id in tuple(self.subscribers):
                        await self.layer.group_add(
                            match_group_name(match_id), self.channel
                        )
            except Exception:
                logger.exception("Match fanout reconciliation failed")

    async def _recover(self, match_id: str, revision: int) -> None:
        if revision <= self.latest.get(match_id, -1):
            return
        # The durable revision is visible before post-commit broker publication.
        # Give that publication one cycle, even when only some caches are ready.
        # Do not restart the grace period as revisions advance during an outage.
        if match_id not in self.recovery_seen:
            self.recovery_seen[match_id] = revision
            return
        snapshot = await self.bootstraps.get(match_id, revision)
        event = {
            "type": "match.changed",
            "match_id": match_id,
            "revision": revision,
            "resources": sorted(ALL_LIVE_RESOURCES),
        }
        if snapshot is not None:
            event.update(snapshot.event)
            live = snapshot.live_state()
            if live is not None:
                event["live"] = live
        await self.publish_shared(event)

    async def _recover_compact(self, match_id: str, revision: int) -> None:
        cohorts = self.compact.get(match_id, {})
        if not any(codec.missing() for codec in cohorts.values()):
            return
        snapshot = await self.compact_bootstraps.get(match_id, revision)
        if snapshot is None:
            return
        event = dict(snapshot.event)
        live = snapshot.live_state()
        if live is not None:
            event["live"] = live
        for resources in list(self.compact.get(match_id, {})):
            await self.prepare_compact(match_id, resources, event)
            if resources in self.compact.get(match_id, {}):
                self.seed_compact(match_id, resources, event)

    def seed_compact(
        self, match_id: str, resources: frozenset[str], event: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep existing viewers on the same base when a handshake fills the cache."""
        codec = self.compact[match_id][resources]
        if (match_id, resources) in self.compact_shared:
            # prepare_compact owns canonical changes through Redis CAS. A richer
            # bootstrap arriving during its throttle must not fork this epoch.
            return codec.snapshot()
        before = codec.sequence
        frame = codec.seed(event)
        if codec.sequence != before:
            for mailbox in self.subscribers.get(match_id, ()):
                if mailbox.resources == resources:
                    mailbox.put(frame)
        return frame

    async def unsubscribe(self, mailbox: Mailbox) -> None:
        """Release broker groups and stop all work after the last viewer leaves."""
        async with self.lock:
            for match_id in mailbox.match_ids:
                viewers = self.subscribers.get(match_id)
                if viewers is None:
                    continue
                viewers.discard(mailbox)
                if mailbox.resources is not None and not any(
                    box.resources == mailbox.resources for box in viewers
                ):
                    self.compact.get(match_id, {}).pop(mailbox.resources, None)
                    self.compact_prepared.pop((match_id, mailbox.resources), None)
                    self.compact_shared.discard((match_id, mailbox.resources))
                if not viewers:
                    del self.subscribers[match_id]
                    self.compact.pop(match_id, None)
                    self.latest.pop(match_id, None)
                    self.recovery_seen.pop(match_id, None)
                    self.bootstraps.forget(match_id)
                    self.compact_bootstraps.forget(match_id)
                    if self.layer is not None:
                        try:
                            await self.layer.group_discard(
                                match_group_name(match_id), self.channel
                            )
                        except Exception:
                            logger.exception("Match fanout group cleanup failed")
            self.leases -= 1
            if self.leases == 0:
                _workers.pop(asyncio.get_running_loop(), None)
                await self.bootstraps.close()
                await self.compact_bootstraps.close()
                tasks = [*self.tasks, *self.initial_tasks]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


def shared_fanout(read_revisions: RevisionReader) -> Fanout:
    """Acquire the event-loop-local worker before awaiting subscription setup."""
    loop = asyncio.get_running_loop()
    worker = _workers.get(loop)
    if worker is None:
        worker = _workers[loop] = Fanout(read_revisions)
    worker.leases += 1
    return worker
