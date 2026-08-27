"""Django storage adapter for generated audio artifacts."""

from __future__ import annotations

from typing import BinaryIO, cast

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


class DjangoAudioStorage:
    """Expose Django's configured default storage through the audio port."""

    def exists(self, key: str) -> bool:
        """Return whether an artifact exists."""
        return default_storage.exists(key)

    def save_bytes(self, key: str, content: bytes) -> str:
        """Persist generated audio bytes."""
        return str(default_storage.save(key, ContentFile(content)))

    def url(self, key: str) -> str:
        """Return the configured storage URL."""
        return default_storage.url(key)

    def open(self, key: str) -> BinaryIO:
        """Open an artifact for streaming."""
        return cast(BinaryIO, default_storage.open(key, "rb"))
