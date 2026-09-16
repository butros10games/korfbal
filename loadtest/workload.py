"""Concurrent spectators and paced authenticated writers over real HTTP/SSE."""

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from http import HTTPStatus
import json
import math
from time import perf_counter
from typing import Any
from uuid import uuid4

import aiohttp

from .generators import run_sharded
from .spectator import Spectator


def distribution(values: list[float]) -> dict[str, float | int | None]:
    """Return nearest-rank percentiles in milliseconds, or null for empty samples."""
    ordered = sorted(values)
    return {
        "count": len(ordered),
        **{
            f"p{percentile}_ms": (
                round(ordered[math.ceil(len(ordered) * percentile / 100) - 1], 2)
                if ordered
                else None
            )
            for percentile in (50, 95, 99)
        },
        "max_ms": round(max(ordered), 2) if ordered else None,
    }


@dataclass
class Metrics:
    """Collect request outcomes separately from successful-response latency."""

    latency: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    outcomes: Counter[str] = field(default_factory=Counter)
    counters: Counter[str] = field(default_factory=Counter)

    def report(self) -> dict[str, Any]:
        """Return JSON-safe aggregate observations, without credentials or payloads."""
        return {
            "latency": {
                name: distribution(values) for name, values in self.latency.items()
            },
            "outcomes": dict(self.outcomes),
            "counters": dict(self.counters),
        }


@dataclass
class Workload:
    """One phase with bounded duration and per-viewer request coalescing."""

    origin: str
    fixtures: list[dict[str, Any]]
    viewers: int
    seconds: int
    interval: float
    reconnect: bool
    live_origin: str | None = None
    sse_origin: str | None = None
    public_origin: str | None = None
    generator_shards: int = 1
    compact: bool = False
    metrics: Metrics = field(default_factory=Metrics)
    commits: dict[tuple[str, int], float] = field(default_factory=dict)
    observations: list[tuple[str, int, float]] = field(default_factory=list)
    latest: dict[int, int] = field(default_factory=dict)
    refreshed: dict[int, int] = field(default_factory=dict)
    live_commits: dict[str, int] = field(default_factory=dict)
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def request(
        self,
        client: aiohttp.ClientSession,
        name: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Measure the full body and count timeouts and non-200s.

        Raises:
            asyncio.CancelledError: The phase ended with a request still in flight.

        """
        start = perf_counter()
        self.metrics.counters["http_requests_started"] += 1
        try:
            async with client.request(
                "POST" if payload is not None else "GET",
                (
                    self.live_origin
                    if name == "live" and self.live_origin
                    else self.public_origin
                    if name in {"live", "summary", "stats", "events", "shots"}
                    and self.public_origin
                    else self.origin
                )
                + path,
                json=payload,
                headers=headers,
                allow_redirects=False,
            ) as response:
                body = await response.read()
                self.metrics.outcomes[f"{name}:{response.status}"] += 1
                self.metrics.counters["response_bytes"] += len(body)
                if response.status == HTTPStatus.OK:
                    elapsed_ms = (perf_counter() - start) * 1000
                    self.metrics.latency[name].append(elapsed_ms)
                    cache_outcome = response.headers.get("X-Korfbal-Live-Cache")
                    if cache_outcome in {"hit", "miss"}:
                        self.metrics.latency[f"{name}_cache_{cache_outcome}"].append(
                            elapsed_ms
                        )
                    server_ms = response.headers.get("X-Korfbal-Request-Duration-Ms")
                    if server_ms is not None:
                        self.metrics.latency[f"{name}_app"].append(float(server_ms))
                        self.metrics.latency[f"{name}_outside_app"].append(
                            max(0, elapsed_ms - float(server_ms))
                        )
                else:
                    self.metrics.counters["http_errors"] += 1
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    self.metrics.counters["invalid_json"] += 1
                    data = {}
                return response.status, data if isinstance(data, dict) else {}
        except asyncio.CancelledError:
            self.metrics.counters["http_requests_cancelled"] += 1
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            self.metrics.outcomes[f"{name}:{type(error).__name__}"] += 1
            self.metrics.counters["http_errors"] += 1
            return 0, {}

    async def writer(
        self, client: aiohttp.ClientSession, fixture: dict[str, Any], deadline: float
    ) -> None:
        """Pace commands and count missed slots instead of sending catch-up bursts."""
        base = f"/api/matches/{fixture['match_id']}/tracker/{fixture['team_id']}"
        csrf = "a" * 32
        headers = {
            "Cookie": f"sessionid={fixture['session']}; csrftoken={csrf}",
            "X-CSRFToken": csrf,
            "Origin": self.origin,
        }
        due = perf_counter()
        sequence = 0
        while due < deadline:
            await asyncio.sleep(max(0, due - perf_counter()))
            started = perf_counter()
            self.metrics.latency["writer_schedule_lag"].append((started - due) * 1000)
            status, state = await self.request(
                client, "tracker_state", base + "/state/", headers=headers
            )
            if status == HTTPStatus.OK and "live_revision" in state:
                payload = {
                    "command": "goal_reg" if sequence % 5 == 0 else "shot_reg",
                    "command_id": str(uuid4()),
                    "expected_revision": state["live_revision"],
                    "player_id": fixture["players"][sequence % len(fixture["players"])],
                    "goal_type": fixture["goal_type"],
                    "for_team": sequence % 3 != 0,
                }
                status, result = await self.request(
                    client,
                    "tracker_command",
                    base + "/commands/",
                    payload=payload,
                    headers=headers,
                )
                if status == HTTPStatus.OK and "live_revision" in result:
                    self.commits[fixture["match_id"], int(result["live_revision"])] = (
                        started
                    )
                    self.metrics.counters["commands_committed"] += 1
                    if "live" in result.get("resources", []):
                        self.live_commits[fixture["match_id"]] = int(
                            result["live_revision"]
                        )
                    self.metrics.latency["tracker_action"].append(
                        (perf_counter() - started) * 1000
                    )
                elif (
                    status == HTTPStatus.CONFLICT
                    and result.get("code") == "revision_conflict"
                ):
                    self.metrics.counters["revision_conflicts"] += 1
            sequence += 1
            due += self.interval
            now = perf_counter()
            if due < now:
                skipped = max(0, math.ceil((min(now, deadline) - due) / self.interval))
                self.metrics.counters["missed_write_slots"] += skipped
                due += skipped * self.interval

    async def run(self) -> dict[str, Any]:
        """Run a timed phase and allow three seconds for final event delivery."""
        if self.generator_shards > 1:
            return await run_sharded(self)
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0),
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as client:
            start = perf_counter()
            deadline = start + self.seconds
            writers = [
                asyncio.create_task(self.writer(client, fixture, deadline))
                for fixture in self.fixtures
            ]
            viewers = [
                asyncio.create_task(
                    Spectator(self, client, index).run(
                        start=start, deadline=deadline + 3
                    )
                )
                for index in range(self.viewers)
            ]

            async def measure_lag() -> None:
                while not self.stop.is_set():
                    before = perf_counter()
                    await asyncio.sleep(0.1)
                    self.metrics.latency["generator_event_loop_lag"].append(
                        max(0, perf_counter() - before - 0.1) * 1000
                    )

            monitor = asyncio.create_task(measure_lag())
            try:
                await asyncio.gather(*writers, *viewers)
            finally:
                self.stop.set()
                for task in (*writers, *viewers, monitor):
                    task.cancel()
                await asyncio.gather(
                    *writers, *viewers, monitor, return_exceptions=True
                )
            return self.report(start)

    def report(self, start: float) -> dict[str, Any]:
        """Audit final viewer revisions and compute combined sample percentiles."""
        for match_id, revision, observed in self.observations:
            committed = self.commits.get((match_id, revision))
            if committed is not None:
                self.metrics.latency["write_start_to_sse"].append(
                    (observed - committed) * 1000
                )
        for index in range(self.viewers):
            match_id = self.fixtures[index % len(self.fixtures)]["match_id"]
            expected = max(
                (revision for match, revision in self.commits if match == match_id),
                default=-1,
            )
            if index not in self.latest or self.latest[index] < expected:
                self.metrics.counters["stale_viewers"] += 1
            if self.refreshed.get(index, -1) < self.live_commits.get(match_id, -1):
                self.metrics.counters["stale_live_reads"] += 1
        return {
            "viewers": self.viewers,
            "matches": len(self.fixtures),
            "seconds": self.seconds,
            "elapsed_seconds": round(perf_counter() - start, 2),
            "write_interval_seconds": self.interval,
            "reconnect": self.reconnect,
            **self.metrics.report(),
        }
