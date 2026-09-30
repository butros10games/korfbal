"""Pytest configuration for the korfbal Django project.

CI runs the korfbal settings with SSL redirect enabled. Most API tests call
endpoints via plain HTTP (the default test client scheme), which would cause
301 redirects and make status code assertions brittle. Process-local storage,
cache, and channel-layer state also need an explicit boundary between tests.

The autouse fixture provides those defaults and cleanup; individual tests can
still override settings explicitly.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
import os
from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache, caches
from pluggy import Result
import pytest
from pytest_django.fixtures import Settings


# The PostgreSQL/Redis lane sets this: a service-backed test that would skip
# (no PostgreSQL, Redis URL or broker) fails instead of passing silently.
REQUIRE_SERVICES = os.environ.get("KORFBAL_REQUIRE_SERVICES") == "1"
SERVICE_MARKER = "service_backed"
WORKER_MARKER = "celery_worker"
PARITY_MARKER = "postgres_parity"


def _clear_shared_test_backends() -> None:
    """Discard process-local state that Django doesn't roll back between tests."""
    cache.clear()
    caches["public_live"].clear()

    channel_layer = get_channel_layer()
    if channel_layer is not None and "flush" in getattr(
        channel_layer, "extensions", ()
    ):
        async_to_sync(channel_layer.flush)()


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker that selects tests for the PostgreSQL/Redis lane."""
    config.addinivalue_line(
        "markers",
        f"{SERVICE_MARKER}: needs PostgreSQL, Redis or a Celery broker; the "
        "service lane (KORFBAL_REQUIRE_SERVICES=1) fails instead of skipping",
    )
    config.addinivalue_line(
        "markers",
        f"{WORKER_MARKER}: spawns Celery workers; runs in its own pytest process "
        "because Celery caches broker and eager settings on first use",
    )
    config.addinivalue_line(
        "markers",
        f"{PARITY_MARKER}: runs on SQLite in the general lane and again on "
        "PostgreSQL, for transactional, locking and query-count behaviour",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Result[pytest.TestReport], None]:
    """Turn a skipped service-backed test into a failure in the service lane."""
    del call
    outcome = yield
    report = outcome.get_result()
    if (
        REQUIRE_SERVICES
        and report.skipped
        and not hasattr(report, "wasxfail")
        and item.get_closest_marker(SERVICE_MARKER) is not None
    ):
        reason = report.longrepr[-1] if isinstance(report.longrepr, tuple) else ""
        report.outcome = "failed"
        report.longrepr = f"Required test service unavailable: {reason}"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Start expensive migration regressions early so xdist can hide their latency."""
    items.sort(
        key=lambda item: item.get_closest_marker("slow_migration") is None,
    )


@pytest.fixture(autouse=True)
def _isolate_test_state(
    settings: Settings,
    tmp_path: Path,
) -> Iterator[None]:
    settings.SECURE_SSL_REDIRECT = False
    settings.MEDIA_ROOT = tmp_path / "media"
    _clear_shared_test_backends()

    try:
        yield
    finally:
        _clear_shared_test_backends()


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """Reject module-level skips before they can hide tests in a required lane."""
    del collector
    report = yield
    if REQUIRE_SERVICES and report.skipped:
        reason = report.longrepr[-1] if isinstance(report.longrepr, tuple) else ""
        report.outcome = "failed"
        report.longrepr = f"Required service collection skipped: {reason}"
    return report
