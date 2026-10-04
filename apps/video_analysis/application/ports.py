"""Outbound capabilities used by video-analysis application services.

Composition roots bind these to private storage, the isolated vision runtime and
bounded CPU inference; services receive only the capability they need.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
import uuid


if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from apps.video_analysis.engine.store import Store
    from apps.video_analysis.models import Workspace


class FrameExtractor(Protocol):
    """Extract frames at recording times into durable private storage."""

    def __call__(self, store: Store, match: dict, times: list[float]) -> list[dict]:
        """Return one stored frame row per requested time."""


class ClipPurger(Protocol):
    """Delete a clip run's artifacts from durable storage and the local cache."""

    def __call__(self, store: Store, run_id: uuid.UUID) -> None:
        """Remove every artifact written for the run."""


class UploadPurger(Protocol):
    """Release an upload's temporary chunks from the workspace's storage."""

    def __call__(self, workspace: Workspace, upload_id: uuid.UUID) -> None:
        """Delete the upload's staged chunks."""


class ProcessingStores(Protocol):
    """Open a leased store that publishes work before evicting bulk files."""

    def __call__(
        self, workspace: Workspace, user: User | None
    ) -> AbstractContextManager[Store]:
        """Return a context manager yielding the processing store."""


class CapacityCheck(Protocol):
    """Report whether the worker's staging budget admits another unit."""

    def __call__(self, store: Store, *, importing: bool) -> bool:
        """Return True when the unit may start now."""


class SourceImporter(Protocol):
    """Import an uploaded or linked recording into private storage."""

    def __call__(self, store: Store, recipe: dict) -> None:
        """Import the source described by the pipeline recipe."""


class BatchInference(Protocol):
    """Run bounded proposal inference outside the Django process."""

    def __call__(
        self, store: Store, model: str, frames: list[dict], output: str
    ) -> dict:
        """Return proposals keyed by frame for the requested batch."""


class ClipRunner(Protocol):
    """Execute one bounded full-clip tracking run."""

    def __call__(self, store: Store, run_id: str, payload: dict) -> None:
        """Run the clip and write its receipt into the store."""


@dataclass(frozen=True, slots=True)
class PipelineRuntime:
    """Capabilities required to advance one review-preparation unit."""

    processing_store: ProcessingStores
    has_capacity: CapacityCheck
    import_source: SourceImporter
    infer_batch: BatchInference
    extract_frames: FrameExtractor
    run_clip: ClipRunner


class IdentityReviewSolver(Protocol):
    """Apply roster answers and crop questions in the isolated vision runtime."""

    def __call__(self, store: Store, run_id: str, request: dict) -> dict:
        """Return the engine's compact review and one receipt per answer."""


@dataclass(frozen=True, slots=True)
class IdentityReviewRuntime:
    """Capabilities required to re-solve a clip run's roster naming."""

    processing_store: ProcessingStores
    solve: IdentityReviewSolver


class MatchPassPublisher(Protocol):
    """Run a finished replay's match pass and rename its published links."""

    def __call__(self, store: Store, record: dict) -> dict:
        """Return the replay's updated receipt (not yet saved)."""


@dataclass(frozen=True, slots=True)
class MatchIdentityRuntime:
    """Capabilities required to republish a recording's match-wide names."""

    processing_store: ProcessingStores
    publish: MatchPassPublisher
