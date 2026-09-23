"""Wire private storage and application jobs at the app boundary."""

from django.conf import settings
from django.contrib.auth.models import User

from apps.video_analysis.adapters import detector
from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.adapters.training import (
    accepted_policy,
    cancel_training,
    launch_status,
    queue_training,
)
from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import StoredFile, Workspace


__all__ = [
    "accepted_policy",
    "cancel_training",
    "launch_status",
    "queue_training",
    "review_store",
    "run_clip",
    "run_detector",
    "sync_workspace_files",
    "worker_store",
]


def review_store(
    user: User, *, hydrate: bool = True
) -> tuple[DatabaseStore, Workspace]:
    """Resolve the configured staff workspace without accepting filesystem paths."""
    workspace = Workspace.objects.get(
        slug=getattr(settings, "VIDEO_ANALYSIS_WORKSPACE", "main")
    )
    return worker_store(workspace, user, hydrate=hydrate), workspace


def worker_store(
    workspace: Workspace, user: User | None, *, hydrate: bool = True
) -> DatabaseStore:
    """Bind background workers to the same authoritative review database."""
    files = (
        WorkspaceObjects(workspace) if settings.VIDEO_ANALYSIS_OBJECT_STORAGE else None
    )
    if files and hydrate:
        files.hydrate_artifacts()
    return DatabaseStore(workspace, user, files)


def sync_workspace_files(workspace: Workspace) -> None:
    """Recover controller outputs and review exports."""
    store = worker_store(workspace, None)
    with store.transaction():
        if store.files:
            store.files.publish_review(store.read())
    store.sync_artifacts()


def run_detector(store: Store, match_id: str, weights: str) -> None:
    """Wire the isolated CPU inference capability."""
    detector.propose(store, match_id, weights)


def run_clip(store: Store, run_id: str, payload: dict) -> None:
    """Wire a bounded full-clip run to the isolated CPU environment."""
    if (
        payload.get("recording_end")
        and isinstance(store, DatabaseStore)
        and store.files
    ):
        root = directory(store, run_id).relative_to(store.root)
        names = [(root / name).as_posix() for name in ("run.json", "cancel.json")]
        for relative in StoredFile.objects.filter(
            workspace_id=store.workspace_id, relative_path__in=names
        ).values_list("relative_path", flat=True):
            store.media(relative)
    detector.clip(store, run_id, payload)
