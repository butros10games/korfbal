"""Pytest configuration for the korfbal Django project.

CI runs the korfbal settings with SSL redirect enabled. Most API tests call
endpoints via plain HTTP (the default test client scheme), which would cause
301 redirects and make status code assertions brittle. Process-local storage,
cache, and channel-layer state also need an explicit boundary between tests.

The autouse fixture provides those defaults and cleanup; individual tests can
still override settings explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
import pytest
from pytest_django.fixtures import SettingsWrapper


def _clear_shared_test_backends() -> None:
    """Discard process-local state that Django doesn't roll back between tests."""
    cache.clear()

    channel_layer = get_channel_layer()
    if channel_layer is not None and "flush" in channel_layer.extensions:
        async_to_sync(channel_layer.flush)()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Start expensive migration regressions early so xdist can hide their latency."""
    items.sort(
        key=lambda item: item.get_closest_marker("slow_migration") is None,
    )


@pytest.fixture(autouse=True)
def _isolate_test_state(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> Iterator[None]:
    settings.SECURE_SSL_REDIRECT = False
    settings.MEDIA_ROOT = tmp_path / "media"
    _clear_shared_test_backends()

    try:
        yield
    finally:
        _clear_shared_test_backends()
