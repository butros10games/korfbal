"""Wire private storage and application jobs at the app boundary."""

from collections.abc import Iterator
from contextlib import contextmanager, suppress
import shutil
import uuid
import zipfile

from django.conf import settings
from django.contrib.auth.models import User

from apps.video_analysis.adapters import detector
from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.adapters.pipeline import (
    extract_batch,
    has_capacity,
    import_source,
    infer_batch,
    purge_uploaded_chunks,
)
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.adapters.training import (
    accepted_policy,
    cancel_training,
    launch_status,
    queue_training,
)
from apps.video_analysis.engine import vision
from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.storage_workspace import (
    clear_incomplete_cache,
    storage_lease,
)
from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import StoredFile, Workspace
from apps.video_analysis.services import label_check


__all__ = [
    "accepted_policy",
    "cancel_training",
    "launch_status",
    "purge_clip",
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
        files.hydrate_artifacts(metadata_only=True)
    return DatabaseStore(workspace, user, files)


@contextmanager
def processing_store(
    workspace: Workspace, user: User | None
) -> Iterator[DatabaseStore]:
    """Publish work before eviction and protect in-use files from the controller.

    Yields:
        A store whose bulk working data is disposable after durable publication.

    """
    store = worker_store(workspace, user, hydrate=False)
    with storage_lease(store.root):
        clear_incomplete_cache(store.root)
        if store.files:
            store.sync_artifacts()
            store.files.evict_bulk()
            store.files.hydrate_artifacts(metadata_only=True)
        try:
            yield store
        finally:
            if store.files:
                # If publication fails, retain local results for sync/retry.
                store.sync_artifacts()
                store.files.evict_bulk()


def sync_workspace_files(workspace: Workspace) -> None:
    """Recover controller outputs without re-downloading the artifact catalogue."""
    with processing_store(workspace, None) as store:
        if store.files:
            store.files.publish_review(store.read())
        label_check.apply(workspace, store)


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
    if isinstance(store, DatabaseStore) and store.files:
        reader = (
            f"vision/runs/{vision.identifier(payload['model'])}/fit/weights/numbers.pt"
        )
        if StoredFile.objects.filter(
            workspace_id=store.workspace_id, relative_path=reader
        ).exists():
            store.media(reader)
    detector.clip(store, run_id, payload)


def import_pipeline_source(store: Store, recipe: dict) -> None:
    """Wire public recording intake to the existing private persistence adapter."""
    import_source(store, recipe)


def infer_pipeline_batch(
    store: Store, model: str, frames: list[dict], output: str
) -> dict:
    """Wire bounded proposal inference without loading detector code in Django."""
    return infer_batch(store, model, frames, output)


def extract_pipeline_frames(
    store: Store, match: dict, times: list[float]
) -> list[dict]:
    """Wire bounded media extraction to private durable storage."""
    return extract_batch(store, match, times)


def intake_store(user: User) -> tuple[DatabaseStore, Workspace]:
    """Initialize a private workspace for the first authenticated intake command."""
    workspace, _ = Workspace.objects.get_or_create(
        slug=getattr(settings, "VIDEO_ANALYSIS_WORKSPACE", "main"),
        defaults={"owner": user},
    )
    return worker_store(workspace, user, hydrate=False), workspace


def pipeline_has_capacity(store: Store, *, importing: bool) -> bool:
    """Wire the worker's local staging budget to pipeline admission."""
    return has_capacity(store, importing=importing)


def purge_upload(store: Store, upload_id: uuid.UUID) -> None:
    """Wire task-owned temporary chunk cleanup to private storage."""
    purge_uploaded_chunks(store, upload_id)


def purge_clip(store: Store, run_id: uuid.UUID) -> None:
    """Wire clip artifact deletion to private storage and the local cache."""
    files = getattr(store, "files", None)
    if files:
        files.purge_clip(run_id)
    root = directory(store, str(run_id))
    for path in [root, *root.parent.glob(f"{root.name}-part-*")]:
        with suppress(FileNotFoundError):
            shutil.rmtree(path)


def snapshot_download(workspace: Workspace, name: str) -> str:
    """Publish a selected export once; browser downloads go directly to private S3."""
    name = vision.identifier(name)
    relative = f"vision/exports/{name}.zip"
    store = worker_store(workspace, None, hydrate=False)
    assert store.files is not None
    if StoredFile.objects.filter(workspace=workspace, relative_path=relative).exists():
        return relative
    with processing_store(workspace, None) as store:
        assert store.files is not None
        store.files.hydrate_prefix(f"vision/snapshots/{name}")
        root = vision.artifact(store, "snapshots", name)
        vision.verify_snapshot(root)
        members = [path for path in root.rglob("*") if path.is_file()]
        store.reserve_working_bytes(
            sum(path.stat().st_size for path in members) + 1024**2
        )
        output = store.root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for member in members:
                    archive.write(member, str(member.relative_to(root.parent)))
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        store.publish_artifact(relative)
        return relative
