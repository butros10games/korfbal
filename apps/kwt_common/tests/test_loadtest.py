"""Protect load-test measurements from false passes and stale-event replays."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from loadtest.__main__ import passes
from loadtest.profile_commands import profile_commands
from loadtest.spectator import Spectator, events
from loadtest.workload import Workload, distribution
import pytest


def phase() -> Workload:
    """Create a minimal in-memory workload without provisioning services."""
    return Workload("http://127.0.0.1", [{"match_id": "synthetic"}], 1, 1, 1, False)


def passing_report() -> dict[str, Any]:
    """Supply a complete successful phase for failure-injection tests."""
    return {
        "latency": {"tracker_command": distribution([100, 200])},
        "counters": {},
        "database": {"job_errors_after": 0, "monitoring_errors": []},
    }


@pytest.mark.parametrize(
    "failure",
    [
        "http_errors",
        "invalid_json",
        "sse_errors",
        "stale_viewers",
        "stale_live_reads",
        "missed_write_slots",
        "http_requests_cancelled",
    ],
)
def test_phase_rejects_failures_even_when_successful_requests_are_fast(
    failure: str,
) -> None:
    """Fast successful requests must not conceal dropped load or stale clients."""
    report = passing_report()
    report["counters"][failure] = 1
    assert not passes(report, 1000)


def test_empty_or_slow_command_samples_do_not_pass() -> None:
    """No commands and excessive latency both reject the capacity claim."""
    report = passing_report()
    assert passes(report, 1000)
    assert not passes(report, 150)
    report["latency"] = {}
    assert not passes(report, 1000)
    assert distribution([])["p95_ms"] is None
    expected_p95 = 95
    assert distribution(list(range(1, 101)))["p95_ms"] == expected_p95


def test_failed_database_monitoring_does_not_pass() -> None:
    """Connection exhaustion must not silently remove measurement evidence."""
    report = passing_report()
    report["database"]["monitoring_errors"] = ["OperationalError"]
    assert not passes(report, 1000)


@pytest.mark.asyncio
async def test_sse_parser_handles_comments_crlf_and_multiline_data() -> None:
    """Parse real streaming frames while ignoring heartbeats and partial tails."""

    async def lines() -> AsyncIterator[bytes]:
        await asyncio.sleep(0)
        for line in (
            b": heartbeat\r\n",
            b"\r\n",
            b"event: ready\r\n",
            b'data: {"revisions":\r\n',
            b'data: {"match": 1}}\r\n',
            b"\r\n",
            b'data: {"partial": true}\n',
        ):
            yield line

    assert [event async for event in events(lines())] == [
        ("ready", {"revisions": {"match": 1}})
    ]


@pytest.mark.asyncio
async def test_viewer_coalesces_resources_and_ignores_old_revisions() -> None:
    """Reconnects and duplicate events do not refetch unmounted tabs or old state."""
    workload = phase()
    async with aiohttp.ClientSession() as client:
        viewer = Spectator(workload, client, 0)
        viewer.pending.clear()
        viewer.observe(
            "match.changed", {"revision": 2, "resources": ["events", "stats"]}, 0
        )
        viewer.observe(
            "match.changed", {"revision": 3, "resources": ["events", "live"]}, 0
        )
        viewer.observe("match.changed", {"revision": 1, "resources": ["summary"]}, 0)
        assert viewer.pending == {"events", "live"}
        viewer.pending.clear()
        viewer.observe("ready", {"revisions": {"synthetic": 3}}, 0)
        assert not viewer.pending
        viewer.observe("ready", {"revisions": {"synthetic": 4}}, 0)
        assert viewer.pending == {"events", "live", "summary"}


@pytest.mark.asyncio
async def test_http_errors_and_malformed_success_responses_are_counted() -> None:
    """Exercise the network measurement boundary, including non-JSON error pages."""

    async def broken(request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.Response(status=int(request.match_info["status"]), text="not json")

    application = web.Application()
    application.router.add_get("/{status}", broken)
    workload = phase()
    async with TestServer(application) as server, aiohttp.ClientSession() as client:
        workload.origin = str(server.make_url("")).rstrip("/")
        await workload.request(client, "live", "/500")
        await workload.request(client, "live", "/200")
    assert workload.metrics.counters["http_errors"] == 1
    expected_invalid = 2
    assert workload.metrics.counters["invalid_json"] == expected_invalid
    assert workload.metrics.outcomes["live:500"] == 1


@pytest.mark.asyncio
async def test_cancelled_http_request_is_recorded_as_unfinished_load() -> None:
    """Ending a phase must not erase in-flight requests from the outcome."""
    arrived = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_request: web.Request) -> web.Response:
        arrived.set()
        await release.wait()
        return web.json_response({})

    application = web.Application()
    application.router.add_get("/", blocked)
    workload = phase()
    async with TestServer(application) as server, aiohttp.ClientSession() as client:
        workload.origin = str(server.make_url("")).rstrip("/")
        task = asyncio.create_task(workload.request(client, "live", "/"))
        await asyncio.wait_for(arrived.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
    assert workload.metrics.counters["http_requests_cancelled"] == 1


def test_command_profile_refuses_non_disposable_database() -> None:
    """Profiling must reject normal settings before loading any match IDs."""
    with pytest.raises(RuntimeError, match="disposable load-test database"):
        profile_commands({})
