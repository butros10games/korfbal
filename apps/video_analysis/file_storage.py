"""Capabilities for private files; annotation services do not know about S3."""

from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol
import uuid


class WorkspaceFiles(Protocol):
    """Durable media, rebuildable cache, and versioned review exports."""

    def import_video(self, relative: str, chunks: Iterable[bytes]) -> dict:
        """Stream and validate a recording without staging a local video."""
        ...

    def evict_bulk(self) -> None:
        """Evict verified bulk files outside an active workspace lease."""
        ...

    def hydrate_prefix(self, relative: str) -> None:
        """Restore only a selected artifact subtree."""
        ...

    def artifact_location(self, relative: str) -> dict:
        """Return the indexed immutable object identity."""
        ...

    def cache_media(self, relative: str) -> Path:
        """Materialize a verified worker input."""
        ...

    def video_source(self, relative: str) -> AbstractContextManager[str]:
        """Read a recording through an authenticated local range bridge."""
        ...

    def media_url(self, relative: str) -> str | None:
        """Sign a short-lived URL for authorized browser playback."""
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

    def purge_clip(self, run_id: uuid.UUID) -> None:
        """Remove every stored artifact of one server-selected clip run."""
        ...
