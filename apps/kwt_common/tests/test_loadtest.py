"""Protect load-test measurements from false passes and stale-event replays."""

import argparse
import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any, cast
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from loadtest.__main__ import passes
from loadtest.profile_commands import profile_commands
from loadtest.profile_payloads import measure, profile_payloads
from loadtest.proxy import cpu_set, validate_cpu_partitions
from loadtest.spectator import Spectator, events
from loadtest.workload import Workload, distribution
import pytest

from apps.game_tracker.adapters.outbound.compact_match import CompactMatch


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


def test_live_budget_requires_successful_samples_below_target() -> None:
    """A passing write budget must not hide slow or missing live reads."""
    report = passing_report()
    assert not passes(report, 1000, 100)
    report["latency"]["live"] = distribution([10, 80])
    assert passes(report, 1000, 100)
    report["latency"]["live"] = distribution([10, 150])
    assert not passes(report, 1000, 100)


@pytest.mark.asyncio
async def test_live_route_uses_isolated_pool_and_records_server_timing() -> None:
    """Keep writes on the primary API and measure the full live response body."""

    async def primary(request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.json_response({"pool": "primary"})

    async def live(request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.json_response(
            {"pool": "live"}, headers={"X-Korfbal-Request-Duration-Ms": "0"}
        )

    api, live_api = web.Application(), web.Application()
    api.router.add_get("/", primary)
    live_api.router.add_get("/", live)
    workload = phase()
    async with (
        TestServer(api) as server,
        TestServer(live_api) as live_server,
        aiohttp.ClientSession() as client,
    ):
        workload.origin = str(server.make_url("")).rstrip("/")
        workload.live_origin = str(live_server.make_url("")).rstrip("/")
        assert (await workload.request(client, "live", "/"))[1] == {"pool": "live"}
        workload.public_origin = workload.live_origin
        for resource in ("summary", "events", "shots", "stats"):
            assert (await workload.request(client, resource, "/"))[1] == {
                "pool": "live"
            }
        assert (await workload.request(client, "tracker_state", "/"))[1] == {
            "pool": "primary"
        }
    assert workload.metrics.latency["live_app"] == [0]
    assert (
        workload.metrics.latency["live_outside_app"] == workload.metrics.latency["live"]
    )


def test_command_profile_refuses_non_disposable_database() -> None:
    """Profiling must reject normal settings before loading any match IDs."""
    with pytest.raises(RuntimeError, match="disposable load-test database"):
        profile_commands({})


def test_push_budget_requires_measured_deliveries() -> None:
    """Fast HTTP reads alone cannot pass the pushed-update latency budget."""
    report = passing_report()
    assert not passes(report, 1000, max_push_p95_ms=100)
    report["latency"]["snapshot_publish_to_receive"] = distribution([20, 90])
    assert passes(report, 1000, max_push_p95_ms=100)
    report["latency"]["snapshot_publish_to_receive"] = distribution([20, 110])
    assert not passes(report, 1000, max_push_p95_ms=100)


def test_pushed_snapshot_replaces_pending_live_fetch() -> None:
    """A viewer applies the pushed revision without scheduling a duplicate GET."""
    workload = phase()
    workload.fixtures = [{"match_id": "match"}]
    revision = 2
    viewer = Spectator(workload, cast(aiohttp.ClientSession, None), 0)
    viewer.observe(
        "match.changed",
        {
            "match_id": "match",
            "revision": 2,
            "resources": ["live", "events"],
            "live": {"match_id": "match", "live_revision": 2},
        },
        0,
    )
    assert "live" not in viewer.pending
    assert "events" in viewer.pending
    assert workload.refreshed[0] == revision
    assert workload.metrics.counters["live_snapshots_pushed"] == 1


@pytest.mark.asyncio
async def test_sharded_viewers_merge_delivery_and_keep_one_writer() -> None:
    """Real child generators preserve all viewer outcomes and final revision checks."""

    async def read(request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.json_response({"live_revision": 1})

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            b'event: ready\ndata: {"revisions":{"synthetic":1}}\n\n'
            b'event: match.changed\ndata: {"match_id":"synthetic",'
            b'"revision":1,"resources":["live"]}\n\n'
        )
        await asyncio.sleep(5)
        return response

    def writer(
        _client: aiohttp.ClientSession, _fixture: dict[str, Any], _deadline: float
    ) -> None:
        workload.commits["synthetic", 1] = perf_counter()
        workload.live_commits["synthetic"] = 1
        workload.metrics.counters["commands_committed"] += 1

    api = web.Application()
    api.router.add_get("/api/live/events/", stream)
    api.router.add_get("/api/matches/synthetic/{resource}/", read)
    workload = phase()
    workload.viewers = 4
    workload.generator_shards = 2
    async with TestServer(api) as server:
        workload.origin = str(server.make_url("")).rstrip("/")
        with patch.object(workload, "writer", side_effect=writer) as write:
            report = await workload.run()
    assert write.call_count == 1
    assert report["counters"]["commands_committed"] == 1
    assert report["counters"]["sse_connections_ready"] == workload.viewers
    assert report["latency"]["write_start_to_sse"]["count"] == workload.viewers
    assert not report["counters"].get("stale_viewers", 0)
    assert not report["counters"].get("stale_live_reads", 0)


def test_public_push_applies_timeline_and_recovers_missing_base() -> None:
    """The load model may suppress reads only after reconstructing a valid delta."""
    workload = phase()
    workload.fixtures = [{"match_id": "match"}]
    viewer = Spectator(workload, cast(aiohttp.ClientSession, None), 0)
    viewer.payloads["events"] = {"identity_version": 3, "events": [{"event_id": "old"}]}
    viewer.revisions["events"] = 1
    update = {
        "mode": "delta",
        "identity_version": 3,
        "live_revision": 2,
        "base_revision": 1,
        "upsert": [{"event_id": "new"}],
        "deleted_ids": ["old"],
        "order": ["new"],
    }
    viewer.observe(
        "match.changed",
        {
            "revision": 2,
            "resources": ["summary", "events"],
            "public_reads": {"summary": {"id_uuid": "match"}, "events": update},
        },
        0,
    )
    assert viewer.payloads["events"]["events"] == [{"event_id": "new"}]
    assert not viewer.pending & {"summary", "events"}
    viewer.observe(
        "match.changed",
        {
            "revision": 4,
            "resources": ["events"],
            "public_reads": {
                "events": {**update, "base_revision": 3, "live_revision": 4}
            },
        },
        0,
    )
    assert "events" in viewer.pending
    assert workload.metrics.counters["public_delta_recoveries"] == 1


def test_starting_snapshot_supplies_bases_without_pending_http() -> None:
    """The harness joins the ready clock to full bases before scheduling GETs."""
    workload = phase()
    workload.fixtures = [{"match_id": "match"}]
    viewer = Spectator(workload, cast(aiohttp.ClientSession, None), 0)
    viewer.observe(
        "ready",
        {
            "revisions": {"match": 1},
            "snapshot_matches": ["match"],
            "live_states": {"match": {"match_id": "match", "live_revision": 1}},
        },
        0,
    )
    assert not viewer.bootstrap_ready.is_set()
    viewer.observe(
        "match.changed",
        {
            "match_id": "match",
            "revision": 1,
            "snapshot": True,
            "resources": ["live", "summary", "events"],
            "public_reads": {
                "summary": {"id_uuid": "match"},
                "events": {"identity_version": 3, "live_revision": 1, "events": []},
            },
        },
        0,
    )
    assert viewer.bootstrap_ready.is_set()
    assert not viewer.pending
    assert workload.refreshed[0] == 1
    assert viewer.payloads["events"]["events"] == []


@pytest.mark.asyncio
async def test_sse_bytes_include_comments_and_frame_delimiters() -> None:
    """Count received SSE body bytes independently from ordinary HTTP bodies."""
    frames = [
        b": heartbeat\r\n",
        b"\r\n",
        b"event: ready\n",
        b'data: {"ok":true}\n',
        b"\n",
    ]

    async def lines() -> AsyncIterator[bytes]:
        for line in frames:
            await asyncio.sleep(0)
            yield line

    counters: Counter[str] = Counter()
    assert [event async for event in events(lines(), counters)] == [
        ("ready", {"ok": True})
    ]
    assert counters == {"sse_response_bytes": sum(map(len, frames))}


@pytest.mark.parametrize(
    ("server", "generator"), [("0", None), (None, "1"), ("0,1", "1,2")]
)
def test_cpu_partitions_reject_missing_or_overlapping_sets(
    server: str | None, generator: str | None
) -> None:
    """Do not label a benchmark isolated if generator/server CPU sets overlap."""
    with pytest.raises(SystemExit):
        validate_cpu_partitions(
            argparse.ArgumentParser(),
            argparse.Namespace(server_cpus=server, generator_cpus=generator),
        )


def test_cpu_partition_normalizes_available_cpus_and_rejects_unavailable() -> None:
    """Fail unavailable affinity early, before starting synthetic services."""
    with patch("loadtest.proxy.os.sched_getaffinity", return_value={0, 1, 2}):
        assert cpu_set("2,0,2") == "0,2"
        with pytest.raises(argparse.ArgumentTypeError):
            cpu_set("3")


def test_payload_profile_refuses_non_disposable_database() -> None:
    """Payload profiling may only execute commands in the owned synthetic DB."""
    with pytest.raises(RuntimeError, match="disposable"):
        profile_payloads({})


def test_payload_reference_prototype_roundtrips_and_counts_new_mapping_cost() -> None:
    """A dictionary reference cannot claim savings by omitting new definitions."""
    match_id = "00000000-0000-4000-8000-000000000001"
    event_id = "00000000-0000-4000-8000-000000000002"
    event = {
        "match_id": match_id,
        "revision": 1,
        "resources": ["events"],
        "public_reads": {
            "events": {"order": [event_id], "upsert": [{"event_id": event_id}]}
        },
    }
    dictionary: dict[str, int] = {}
    first = measure(event, dictionary)
    repeated = measure(event, dictionary)
    expected_entries = 2
    assert first["short_reference_roundtrip"] is True
    assert first["new_dictionary_entries"] == expected_entries
    assert repeated["new_dictionary_entries"] == 0
    assert (
        first["short_reference_envelope_json_bytes"]
        > repeated["short_reference_envelope_json_bytes"]
    )
    assert (
        first["public_resource_json_bytes"]["events"]
        > first["timeline_order_json_bytes"]["events"]
    )


@pytest.mark.asyncio
async def test_compact_timing_separates_publication_delivery_and_decode() -> None:
    """Time distinct stages without dropping the original end-to-end samples."""
    workload = phase()
    workload.compact = True
    codec = CompactMatch("synthetic", frozenset({"live"}))

    def event(revision: int) -> dict[str, Any]:
        return {
            "match_id": "synthetic",
            "revision": revision,
            "resources": ["live"],
            "live": {
                "match_id": "synthetic",
                "live_revision": revision,
                "timer": {"type": "deactivated"},
            },
        }

    with patch(
        "apps.game_tracker.adapters.outbound.compact_match.time", return_value=1000.02
    ):
        initial = codec.seed({**event(0), "snapshot": True})["_wire"]
        update = codec.publish({**event(1), "published_at": 1000})["_wire"]

    async def stream(request: web.Request) -> web.Response:
        assert await request.read() == b""
        assert request.query["compact"] == "1"
        return web.Response(body=initial + update, content_type="text/event-stream")

    app = web.Application()
    app.router.add_get("/api/live/events/", stream)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        workload.origin = str(server.make_url("")).rstrip("/")
        viewer = Spectator(workload, client, 0)
        with (
            patch("loadtest.spectator.time", return_value=1000.05),
            pytest.raises(RuntimeError, match="ended early"),
        ):
            await viewer.receive(perf_counter() + 2)
    latency = workload.metrics.latency
    assert latency["publication_to_compact_encode"] == pytest.approx([20])
    assert latency["compact_encode_to_receive"] == pytest.approx([30])
    assert latency["snapshot_publish_to_receive"] == pytest.approx([50])
    assert len(latency["compact_live_decode"]) == 1
    assert latency["compact_live_decode"][0] >= 0
