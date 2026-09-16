"""SSE consumption independent of the viewer's coalesced HTTP refetch loop."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterable, AsyncIterator
from http import HTTPStatus
import json
from time import perf_counter, time
from typing import TYPE_CHECKING, Any

import aiohttp

from .compact import CompactDecoder


TIMELINE_IDENTITY = 3


if TYPE_CHECKING:
    from .workload import Workload


async def events(
    lines: AsyncIterable[bytes],
    counters: Counter[str] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any] | list[Any]]]:
    """Parse complete SSE frames, including comments and multiline data.

    Yields:
        Event names and decoded JSON objects.

    """
    name = "message"
    data = []
    async for raw in lines:
        if counters is not None:
            counters["sse_response_bytes"] += len(raw)
        line = raw.decode().rstrip("\r\n")
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line:
            if data:
                yield name, json.loads("\n".join(data))
            data.clear()
            name = "message"


class Spectator:
    """A viewer watches live/summary and one mounted events, shots or stats tab."""

    def __init__(
        self, workload: Workload, client: aiohttp.ClientSession, index: int
    ) -> None:
        """Keep revision and refetch state private to this simulated browser."""
        self.workload = workload
        self.client = client
        self.index = index
        self.match_id = workload.fixtures[index % len(workload.fixtures)]["match_id"]
        self.mounted = {"live", "summary", ("events", "shots", "stats")[index % 3]}
        self.revisions: dict[str, int] = {}
        self.payloads: dict[str, dict[str, Any]] = {}
        self.pending = set(self.mounted)
        self.bootstrap_live: dict[str, Any] | None = None
        self.bootstrap_ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.changed.set()

    async def fetch(self, resource: str) -> None:
        """Request deltas only after a successful initial timeline snapshot."""
        suffix = ""
        if resource in {"events", "shots"}:
            suffix = "?identity_version=3"
            if resource in self.revisions:
                suffix += f"&since_revision={self.revisions[resource]}"
        before = self.revisions.get(resource, -1)
        status, data = await self.workload.request(
            self.client,
            resource,
            f"/api/matches/{self.match_id}/{resource}/{suffix}",
        )
        if self.revisions.get(resource, -1) > before:
            return
        if status == HTTPStatus.OK:
            if data.get("mode") == "delta":
                data = self.merge_delta(resource, data) or {}
            if data:
                self.payloads[resource] = data
        if status == HTTPStatus.OK and "live_revision" in data:
            self.revisions[resource] = max(
                self.revisions.get(resource, -1), int(data["live_revision"])
            )
            if resource == "live":
                self.workload.refreshed[self.index] = self.revisions[resource]

    async def refetch(self) -> None:
        """Coalesce changes arriving during a read into the next resource refresh."""
        try:
            await asyncio.wait_for(
                self.bootstrap_ready.wait(), 1.5 + (self.index % 101) / 200
            )
        except TimeoutError:
            self.workload.metrics.counters["bootstrap_http_fallbacks"] += 1
        while True:
            await self.changed.wait()
            self.changed.clear()
            resources = sorted(self.pending)
            self.pending.clear()
            await asyncio.gather(*(self.fetch(resource) for resource in resources))

    def observe(self, name: str, event: dict[str, Any], started: float) -> None:
        """Apply monotonic revision notifications without fetching unmounted tabs."""
        metrics = self.workload.metrics
        if name == "ready":
            metrics.latency["sse_ready"].append((perf_counter() - started) * 1000)
            metrics.counters["sse_connections_ready"] += 1
            revision = event["revisions"].get(self.match_id, -1)
            resources = self.mounted
            if self.match_id in event.get("snapshot_matches", []):
                self.bootstrap_live = event.get("live_states", {}).get(self.match_id)
                return
        elif name == "match.changed":
            revision = event["revision"]
            if event.get("snapshot") and self.bootstrap_live:
                if self.bootstrap_live.get("live_revision") == revision:
                    event = {**event, "live": self.bootstrap_live}
                self.bootstrap_live = None
            resources = set(event["resources"]) & self.mounted
            self.workload.observations.append((self.match_id, revision, perf_counter()))
            metrics.counters["sse_changes"] += 1
        else:
            return
        previous_revision = self.workload.latest.get(self.index, -1)
        if revision > previous_revision or (
            (event.get("snapshot") or self.workload.compact)
            and revision == previous_revision
        ):
            self.workload.latest[self.index] = revision
            resources = self.apply_public_reads(event, set(resources), revision)
            live = event.get("live")
            if (
                isinstance(live, dict)
                and live.get("match_id") == self.match_id
                and live.get("live_revision") == revision
            ):
                resources = set(resources) - {"live"}
                self.pending.discard("live")
                self.revisions["live"] = max(self.revisions.get("live", -1), revision)
                self.workload.refreshed[self.index] = self.revisions["live"]
                metrics.counters["live_snapshots_pushed"] += 1
                published_at = event.get("published_at")
                if isinstance(published_at, (int, float)):
                    metrics.latency["snapshot_publish_to_receive"].append(
                        max(0, (time() - published_at) * 1000)
                    )
            self.pending.update(resources)
            if event.get("snapshot"):
                self.bootstrap_ready.set()
                metrics.counters["public_snapshots_received"] += 1
            self.changed.set()

    def apply_public_reads(
        self, event: dict[str, Any], resources: set[str], revision: int
    ) -> set[str]:
        """Use compatible pushed resources instead of scheduling HTTP reads."""
        metrics = self.workload.metrics
        for resource, incoming in event.get("public_reads", {}).items():
            payload = incoming
            if resource not in self.mounted or resource not in resources:
                continue
            if resource == "summary" and payload.get("id_uuid") != self.match_id:
                continue
            if resource in {"events", "shots"}:
                if (
                    payload.get("live_revision") != revision
                    or payload.get("identity_version") != TIMELINE_IDENTITY
                ):
                    continue
                if payload.get("mode") == "delta":
                    payload = self.merge_delta(resource, payload)
                    if payload is None:
                        metrics.counters["public_delta_recoveries"] += 1
                        continue
            resources = set(resources) - {resource}
            self.pending.discard(resource)
            self.payloads[resource] = payload
            self.revisions[resource] = revision
            metrics.counters[f"{resource}_updates_pushed"] += 1
        return resources

    def merge_delta(
        self, resource: str, delta: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Model the browser's base, identity, deletion and order validation."""
        previous = self.payloads.get(resource)
        if (
            previous is None
            or previous.get("identity_version") != TIMELINE_IDENTITY
            or self.revisions.get(resource, -1) < delta["base_revision"]
        ):
            return None
        items = {
            item["event_id"]: item
            for item in previous.get(resource, [])
            if item["event_id"] not in delta["deleted_ids"]
        }
        items.update({item["event_id"]: item for item in delta["upsert"]})
        if any(item_id not in items for item_id in delta["order"]):
            return None
        return {
            **{
                key: value
                for key, value in delta.items()
                if key
                not in {"mode", "base_revision", "upsert", "deleted_ids", "order"}
            },
            resource: [items[item_id] for item_id in delta["order"]],
        }

    async def receive(self, until: float) -> None:
        """Consume one connection until its scheduled disconnect.

        Raises:
            RuntimeError: The server rejects or prematurely closes the stream.

        """
        started = perf_counter()
        decoder = CompactDecoder(self.match_id)
        suffix = (
            "&compact=1&resources=" + ",".join(sorted(self.mounted))
            if self.workload.compact
            else ""
        )
        async with (
            asyncio.timeout(max(0.001, until - started)),
            self.client.get(
                (self.workload.sse_origin or self.workload.origin)
                + f"/api/live/events/?match_ids={self.match_id}&snapshot=1"
                + suffix,
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=10, sock_read=25
                ),
                allow_redirects=False,
            ) as response,
        ):
            if response.status != HTTPStatus.OK:
                raise RuntimeError(f"SSE returned HTTP {response.status}")
            async for name, event in events(
                response.content, self.workload.metrics.counters
            ):
                if name == "match.compact":
                    assert isinstance(event, list)
                    received_at = time()
                    decode_started = perf_counter()
                    decoded = decoder.decode(event)
                    decode_ms = (perf_counter() - decode_started) * 1000
                    if decoded is None:
                        continue
                    self.workload.metrics.counters["compact_frames_received"] += 1
                    # Same frames as publication latency, before state reconstruction.
                    # Receive follows SSE/JSON parsing, not the socket syscall.
                    if decoded.get("live") and isinstance(event[11], (int, float)):
                        latency = self.workload.metrics.latency
                        latency["publication_to_compact_encode"].append(
                            max(0, (event[12] - event[11]) * 1000)
                        )
                        latency["compact_encode_to_receive"].append(
                            max(0, (received_at - event[12]) * 1000)
                        )
                        latency["compact_live_decode"].append(decode_ms)
                    self.observe("match.changed", decoded, started)
                else:
                    assert isinstance(event, dict)
                    self.observe(name, event, started)
            raise RuntimeError("SSE stream ended early")

    async def run(self, *, start: float, deadline: float) -> None:
        """Stagger arrivals and optionally reconnect everyone halfway through."""
        await asyncio.sleep(
            min(2.0, self.workload.seconds / 5)
            * self.index
            / max(1, self.workload.viewers)
        )
        fetch_task = asyncio.create_task(self.refetch())
        reconnect_at = (
            start + self.workload.seconds / 2 if self.workload.reconnect else deadline
        )
        try:
            while perf_counter() < deadline:
                until = reconnect_at if perf_counter() < reconnect_at else deadline
                try:
                    await self.receive(until)
                except TimeoutError:
                    if perf_counter() < until - 0.1:
                        self.workload.metrics.counters["sse_errors"] += 1
                except (aiohttp.ClientError, RuntimeError, ValueError):
                    self.workload.metrics.counters["sse_errors"] += 1
                    await asyncio.sleep(0.5 + self.index % 10 / 20)
        finally:
            if fetch_task.done() and not fetch_task.cancelled():
                # Surface refetch failures instead of hiding them during cleanup.
                fetch_task.result()
            fetch_task.cancel()
            await asyncio.gather(fetch_task, return_exceptions=True)
