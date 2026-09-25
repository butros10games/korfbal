"""Capabilities for private files; annotation services do not know about S3."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol
import uuid


class WorkspaceFiles(Protocol):
    """Durable media, rebuildable cache, and versioned review exports."""

    def cache_media(self, relative: str) -> Path:
        """Materialize a verified worker input."""
        ...

    def size(self, relative: str) -> int:
        """Read registered object length."""
        ...

    def chunks(self, relative: str, start: int, end: int) -> Iterator[bytes]:
        """Stream an authenticated byte range."""
        ...

    def publish_review(self, data: dict[str, Any]) -> None:
        """Persist media and a revision-specific annotation export."""
        ...

    def publish_media(self, relative: str) -> None:
        """Persist one registered worker image."""
        ...

    def sync_artifacts(self) -> None:
        """Publish changed working artifacts."""
        ...

    def publish_artifact(self, relative: str) -> None:
        """Publish one interactive metadata change."""
        ...

    def hydrate_artifacts(self, *, metadata_only: bool = False) -> None:
        """Restore missing artifact cache files."""
        ...

    def purge_upload(self, upload_id: uuid.UUID) -> None:
        """Remove only temporary chunks for a server-selected upload session."""
        ...
