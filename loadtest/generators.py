"""Independent spectator processes with one coordinated writer and revision audit."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from time import perf_counter
from typing import TYPE_CHECKING, Any

import aiohttp

from .spectator import Spectator


if TYPE_CHECKING:
    from .workload import Workload


async def _viewers(
    workload: Workload, indexes: range, start: float, client_host: str
) -> dict[str, Any]:
    deadline = start + workload.seconds + 3
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0, local_addr=(client_host, 0)),
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as client:

        async def lag() -> None:
            while True:
                before = perf_counter()
                await asyncio.sleep(0.1)
                workload.metrics.latency["generator_event_loop_lag"].append(
                    max(0, perf_counter() - before - 0.1) * 1000
                )

        monitor = asyncio.create_task(lag())
        try:
            await asyncio.gather(
                *(
                    Spectator(workload, client, index).run(
                        start=start,
                        deadline=deadline,
                    )
                    for index in indexes
                )
            )
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
    return {
        "latency": dict(workload.metrics.latency),
        "outcomes": workload.metrics.outcomes,
        "counters": workload.metrics.counters,
        "observations": workload.observations,
        "latest": workload.latest,
        "refreshed": workload.refreshed,
    }


def _child(
    factory: type[Workload],
    options: dict[str, Any],
    indexes: range,
    pipe: Connection,
    client_host: str,
) -> None:
    try:
        workload = factory(**options)
        pipe.send("ready")
        start = pipe.recv()
        pipe.send(asyncio.run(_viewers(workload, indexes, start, client_host)))
    finally:
        pipe.close()


async def run_sharded(workload: Workload) -> dict[str, Any]:
    """Partition viewers; merge raw samples and audit them against real commits.

    Raises:
        RuntimeError: A generator fails before readiness or completion.
        TypeError: A generator returns an invalid report.

    """
    context = multiprocessing.get_context("spawn")
    count = min(workload.generator_shards, workload.viewers)
    options = {
        key: getattr(workload, key)
        for key in (
            "origin",
            "viewers",
            "seconds",
            "interval",
            "reconnect",
            "live_origin",
            "sse_origin",
            "public_origin",
            "compact",
        )
    }
    # Spectators receive only public match IDs; writer sessions stay in the parent.
    options["fixtures"] = [{"match_id": item["match_id"]} for item in workload.fixtures]
    processes = []
    pipes = []
    try:
        for index in range(count):
            parent, child = context.Pipe()
            process = context.Process(
                target=_child,
                args=(
                    type(workload),
                    options,
                    range(index, workload.viewers, count),
                    child,
                    f"127.0.0.{index + 2}",
                ),
                daemon=True,
            )
            process.start()
            child.close()
            processes.append(process)
            pipes.append(parent)

        ready = await asyncio.gather(*(_receive(pipe, 30) for pipe in pipes))
        if ready != ["ready"] * count:
            raise RuntimeError("Invalid spectator generator readiness")
        start = perf_counter()
        for pipe in pipes:
            pipe.send(start)
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0),
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as client:
            reports = asyncio.gather(
                *(_receive(pipe, workload.seconds + 35) for pipe in pipes)
            )
            try:
                await asyncio.gather(
                    *(
                        workload.writer(client, fixture, start + workload.seconds)
                        for fixture in workload.fixtures
                    )
                )
                for report in await reports:
                    if not isinstance(report, dict):
                        raise TypeError("Invalid spectator generator report")
                    for name, values in report["latency"].items():
                        workload.metrics.latency[name].extend(values)
                    workload.metrics.outcomes.update(report["outcomes"])
                    workload.metrics.counters.update(report["counters"])
                    workload.observations.extend(report["observations"])
                    workload.latest.update(report["latest"])
                    workload.refreshed.update(report["refreshed"])
            finally:
                reports.cancel()
                await asyncio.gather(reports, return_exceptions=True)
        return workload.report(start)
    finally:
        _stop_processes(processes, pipes)


async def _receive(pipe: Connection, max_wait: float) -> str | dict[str, Any]:
    if not await asyncio.to_thread(pipe.poll, max_wait):
        raise RuntimeError("Spectator generator did not respond")
    try:
        return pipe.recv()
    except EOFError as error:
        raise RuntimeError("Spectator generator exited before reporting") from error


def _stop_processes(
    processes: Sequence[BaseProcess], pipes: Sequence[Connection]
) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()
    for pipe in pipes:
        pipe.close()
