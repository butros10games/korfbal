"""Regression coverage for process-local test isolation."""

from __future__ import annotations

from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, storages
import pytest
from pytest_django.fixtures import SettingsWrapper


pytestmark = pytest.mark.xdist_group(name="test-isolation-regression")


def test_media_storage_uses_each_tests_temporary_media_root(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    """The autouse fixture gives default storage a unique location per test."""
    expected_media_root = tmp_path / "media"
    default_storage = storages["default"]

    assert expected_media_root == settings.MEDIA_ROOT
    assert isinstance(default_storage, FileSystemStorage)
    assert Path(default_storage.location) == expected_media_root

    saved_name = default_storage.save("audit/example.txt", ContentFile(b"isolated"))

    assert default_storage.open(saved_name).read() == b"isolated"
    assert (expected_media_root / saved_name).is_file()


def test_shared_backend_cleanup_1_leaves_process_local_state() -> None:
    """Leave state behind so the following test exercises fixture cleanup."""
    channel_layer = get_channel_layer()
    assert channel_layer is not None
    channel_name = async_to_sync(channel_layer.new_channel)("audit.")

    cache.set("audit:test-isolation", "stale")
    async_to_sync(channel_layer.send)(channel_name, {"type": "audit.message"})


def test_shared_backend_cleanup_2_discards_process_local_state() -> None:
    """Autouse setup discards cache entries and queued channel messages."""
    channel_layer = get_channel_layer()
    assert channel_layer is not None

    assert cache.get("audit:test-isolation") is None
    assert channel_layer.channels == {}
    assert channel_layer.groups == {}
