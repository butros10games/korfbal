"""Bounded, idempotent file intake; untrusted bytes never become public media."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import unicodedata
import uuid

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from apps.kwt_common.services.jobs import enqueue
from apps.video_analysis.composition import purge_upload, worker_store
from apps.video_analysis.engine.store import ConflictError, Store
from apps.video_analysis.models import ReviewPipeline, VideoUpload, Workspace
from apps.video_analysis.services.pipeline import ACTIVE, MAX_PIPELINES, wake


CHUNK_SIZE = 8 * 1024 * 1024
MAX_SIZE = 6_000_000_000
MAX_OPEN = 10
MAX_NAME = 200


@dataclass(frozen=True)
class Chunk:
    """One bounded binary request with its sequential position."""

    index: int
    data: bytes


def receipt(row: VideoUpload) -> dict:
    """Return progress without storage keys or chunk contents."""
    return {
        "id": str(row.pk),
        "received": row.received,
        "size": row.size,
        "chunk_size": CHUNK_SIZE,
        "status": row.status,
    }


def cleanup_later(row: VideoUpload, at: datetime | None = None) -> None:
    """Persist cleanup intent independently of a browser or API process."""
    enqueue(
        "apps.video_analysis.tasks.cleanup_upload",
        str(row.pk),
        args=[str(row.pk)],
        queue="vision",
        due_at=at or row.expires_at,
    )


@transaction.atomic
def begin(workspace: Workspace, actor: User, payload: dict) -> dict:
    """Bind a stable request ID to one owner's immutable upload declaration.

    Raises:
        ValueError: The filename, size or admission budget is invalid.
        ConflictError: A request ID was reused for a different file or owner.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    key = uuid.UUID(str(payload["request_id"]))
    name, size = payload.get("name"), payload.get("size")
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME:
        raise ValueError("Choose a file with a valid filename")
    if (
        Path(name).name != name
        or "\\" in name
        or any(unicodedata.category(c).startswith("C") for c in name)
        or Path(name).suffix.lower() not in {".mp4", ".webm"}
    ):
        raise ValueError("Choose an MP4 or WebM file up to 6 GB")
    if type(size) is not int or not 1 <= size <= MAX_SIZE:
        raise ValueError("Choose a video up to 6 GB")
    row = VideoUpload.objects.filter(pk=key).first()
    if row:
        if (row.workspace_id, row.requested_by_id, row.name, row.size) != (
            workspace.pk,
            actor.pk,
            name,
            size,
        ):
            raise ConflictError("Upload request ID already belongs to another file")
        return receipt(row)
    if ReviewPipeline.objects.filter(pk=key).exists():
        raise ConflictError("Request ID already belongs to a preparation")
    if (
        VideoUpload.objects.filter(
            workspace=workspace, status="receiving", expires_at__gt=timezone.now()
        ).count()
        >= MAX_OPEN
    ):
        raise ValueError("Finish or cancel an existing upload first")
    row = VideoUpload.objects.create(
        id=key,
        workspace=workspace,
        requested_by=actor,
        name=name,
        size=size,
        expires_at=timezone.now() + timedelta(days=7),
    )
    cleanup_later(row)
    return receipt(row)


def owned(workspace: Workspace, actor: User, key: str) -> VideoUpload:
    """Only the initiating account can mutate or inspect an upload session."""
    return VideoUpload.objects.select_for_update().get(
        pk=uuid.UUID(str(key)), workspace=workspace, requested_by=actor
    )


def receiving(row: VideoUpload) -> None:
    """Reject late writes after closure or expiration.

    Raises:
        ConflictError: The upload is no longer writable.

    """
    if row.status != "receiving" or row.expires_at <= timezone.now():
        raise ConflictError("Upload closed or expired; choose the file again")


@transaction.atomic
def part(
    workspace: Workspace,
    actor: User,
    store: Store,
    *,
    key: str,
    chunk: Chunk,
) -> dict:
    """Verify a bounded sequential chunk and durably publish before acknowledging.

    Raises:
        ValueError: The chunk size, order or file signature is invalid.
        ConflictError: A retry contains different bytes.

    """
    row = owned(workspace, actor, key)
    receiving(row)
    index, data = chunk.index, chunk.data
    if index < 0 or index > len(row.parts):
        raise ValueError("Upload chunks must arrive in order")
    expected = min(CHUNK_SIZE, row.size - index * CHUNK_SIZE)
    if expected <= 0 or len(data) != expected:
        raise ValueError("Upload chunk size does not match the declared file")
    if index == 0:
        valid = (
            data[4:8] == b"ftyp"
            if row.name.lower().endswith(".mp4")
            else data[:4] == b"\x1aE\xdf\xa3"
        )
        if not valid:
            raise ValueError("The file contents do not match MP4 or WebM")
    checksum = hashlib.sha256(data).hexdigest()
    if index < len(row.parts):
        if row.parts[index]["sha256"] != checksum:
            raise ConflictError("Retry differs from the previously uploaded chunk")
        return receipt(row)
    relative = f"uploads/{row.pk}/{index:06d}-{checksum}.part"
    path = store.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    store.publish_media(relative)
    if getattr(store, "files", None):
        path.unlink(missing_ok=True)
    row.parts = [*row.parts, {"path": relative, "sha256": checksum, "size": len(data)}]
    row.received += len(data)
    row.save(update_fields=["parts", "received"])
    return receipt(row)


@transaction.atomic
def finish(workspace: Workspace, actor: User, key: str) -> dict:
    """Queue validation/import only after every declared byte is durably stored.

    Raises:
        ValueError: Bytes are missing or the preparation queue is full.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    row = owned(workspace, actor, key)
    if row.status in {"queued", "consumed"}:
        return receipt(row)
    receiving(row)
    if row.received != row.size:
        raise ValueError("Finish uploading the entire file first")
    if (
        ReviewPipeline.objects.filter(workspace=workspace, status__in=ACTIVE).count()
        >= MAX_PIPELINES
    ):
        raise ValueError("Preparation queue full; retry when a slot is available")
    ReviewPipeline.objects.create(
        id=row.pk,
        workspace=workspace,
        requested_by=actor,
        recipe={
            "version": 1,
            "stage": "import",
            "source_type": "upload",
            "match_id": f"intake-{row.pk.hex}",
            "upload": {"name": row.name, "size": row.size, "parts": row.parts},
        },
    )
    row.status = "queued"
    row.save(update_fields=["status"])
    wake(workspace.pk)
    return receipt(row)


@transaction.atomic
def cancel(workspace: Workspace, actor: User, key: str) -> dict:
    """Discard only unfinished intake; an accepted import remains durable.

    Raises:
        ConflictError: Processing already owns the upload.

    """
    row = owned(workspace, actor, key)
    if row.status in {"queued", "consumed"}:
        raise ConflictError("Upload is already queued; use preparation controls")
    row.status = "cancelled"
    row.save(update_fields=["status"])
    cleanup_later(row, timezone.now())
    return receipt(row)


@transaction.atomic
def cleanup(key: str) -> None:
    """Release temporary chunks after successful import, cancellation or expiration."""
    row = VideoUpload.objects.select_for_update().filter(pk=uuid.UUID(str(key))).first()
    if row is None or row.status == "consumed":
        return
    if row.status == "receiving" and row.expires_at > timezone.now():
        cleanup_later(row)
        return
    if (
        row.status == "queued"
        and not ReviewPipeline.objects.filter(
            pk=row.pk, status__in=["awaiting_cuts", "prepared"]
        ).exists()
    ):
        cleanup_later(row, timezone.now() + timedelta(days=1))
        return
    purge_upload(worker_store(row.workspace, None, hydrate=False), row.pk)
    row.status = (
        "consumed"
        if row.status == "queued"
        else "expired"
        if row.status == "receiving"
        else row.status
    )
    row.parts = []
    row.save(update_fields=["status", "parts"])
