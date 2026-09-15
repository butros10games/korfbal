"""SSE consumption independent of the viewer's coalesced HTTP refetch loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from http import HTTPStatus
import json
from time import perf_counter
from typing import TYPE_CHECKING, Any

import aiohttp


if TYPE_CHECKING:
    from .workload import Workload


async def events(
    lines: AsyncIterable[bytes],
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Parse complete SSE frames, including comments and multiline data.

    Yields:
        Event names and decoded JSON objects.

    """
    name = "message"
    data = []
    async for raw in lines:
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
        self.pending = set(self.mounted)
        self.changed = asyncio.Event()
        self.changed.set()

    async def fetch(self, resource: str) -> None:
        """Request deltas only after a successful initial timeline snapshot."""
        suffix = ""
        if resource in {"events", "shots"}:
            suffix = "?identity_version=3"
            if resource in self.revisions:
                suffix += f"&since_revision={self.revisions[resource]}"
        status, data = await self.workload.request(
            self.client,
            resource,
            f"/api/matches/{self.match_id}/{resource}/{suffix}",
        )
        if status == HTTPStatus.OK and "live_revision" in data:
            self.revisions[resource] = int(data["live_revision"])
            if resource == "live":
                self.workload.refreshed[self.index] = int(data["live_revision"])

    async def refetch(self) -> None:
        """Coalesce changes arriving during a read into the next resource refresh."""
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
        elif name == "match.changed":
            revision = event["revision"]
            resources = set(event["resources"]) & self.mounted
            self.workload.observations.append((self.match_id, revision, perf_counter()))
            metrics.counters["sse_changes"] += 1
        else:
            return
        if revision > self.workload.latest.get(self.index, -1):
            self.workload.latest[self.index] = revision
            self.pending.update(resources)
            self.changed.set()

    async def receive(self, until: float) -> None:
        """Consume one connection until its scheduled disconnect.

        Raises:
            RuntimeError: The server rejects or prematurely closes the stream.

        """
        started = perf_counter()
        async with (
            asyncio.timeout(max(0.001, until - started)),
            self.client.get(
                self.workload.origin + f"/api/live/events/?match_ids={self.match_id}",
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=10, sock_read=25
                ),
                allow_redirects=False,
            ) as response,
        ):
            if response.status != HTTPStatus.OK:
                raise RuntimeError(f"SSE returned HTTP {response.status}")
            async for name, event in events(response.content):
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
