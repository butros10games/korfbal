"""Django storage adapter for generated audio artifacts."""

from __future__ import annotations

from hashlib import sha256
from typing import BinaryIO, cast

from django.core.cache import cache
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


# Longer than one resize; a crashed worker's reservation lapses on its own.
VARIANT_CLAIM_SECONDS = 60


def _claim_key(key: str) -> str:
    return f"image-variant:{sha256(key.encode()).hexdigest()}"


class DjangoImageVariantStorage:
    """Expose Django's configured default storage through the image-variant port."""

    def read(self, key: str) -> bytes:
        """Return a stored object's bytes."""
        with default_storage.open(key, "rb") as stored:
            return stored.read()

    def write(self, key: str, content: bytes) -> None:
        """Write at the exact key; ``save`` renames it after a concurrent write."""
        with default_storage.open(key, "wb") as stored:
            stored.write(content)

    def exists(self, key: str) -> bool:
        """Return whether an object is stored."""
        return default_storage.exists(key)

    def delete(self, key: str) -> None:
        """Remove an object if it exists."""
        default_storage.delete(key)

    def claim(self, key: str) -> bool:
        """Reserve creation across workers through the shared cache."""
        return cache.add(_claim_key(key), 1, timeout=VARIANT_CLAIM_SECONDS)

    def release(self, key: str) -> None:
        """End a reservation made by ``claim``."""
        cache.delete(_claim_key(key))
