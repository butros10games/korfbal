"""Replace stored MP4 recordings with a losslessly regrouped copy."""

import hashlib
import logging
from pathlib import Path
import shutil
import tempfile
import uuid

from django.conf import settings
from django.db import transaction

from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.engine.container import (
    index_is_compact,
    repackage,
    verify_repackaged,
)
from apps.video_analysis.models import StoredFile
from apps.video_analysis.services import repackaging


logger = logging.getLogger(__name__)


def _checksum(files: WorkspaceObjects, bucket: str, key: str) -> tuple[int, str]:
    """Hash bytes read back from storage, including multipart uploads.

    Returns:
        Size and SHA-256 of the stored object.

    """
    response = files.client.get_object(Bucket=bucket, Key=key)
    digest, size = hashlib.sha256(), 0
    with response["Body"] as body:
        for chunk in iter(lambda: body.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def repackage_video(files: WorkspaceObjects, relative: str) -> str | None:
    """Replace an MP4 with a losslessly regrouped copy that opens quickly.

    The encoded packets and their timestamps are verified to be identical. The
    superseded object stays readable for jobs already streaming it; delete it
    later with ``delete_unreferenced``.

    Returns:
        The superseded object key, or None when nothing was replaced.

    Raises:
        ValueError: The download or the repackaged file failed verification.

    """
    files.path(relative)
    record = StoredFile.objects.get(workspace=files.workspace, relative_path=relative)
    if (
        Path(relative).suffix.lower() != ".mp4"
        or record.bucket != settings.VIDEO_ANALYSIS_MEDIA_BUCKET
    ):
        return None
    client = files.read_client(record)

    def read(start: int, end: int) -> bytes:
        response = client.get_object(
            Bucket=record.bucket, Key=record.object_key, Range=f"bytes={start}-{end}"
        )
        with response["Body"] as body:
            return body.read()

    if index_is_compact(read, record.size):
        return None
    files.root.mkdir(parents=True, exist_ok=True)
    needed = 2 * record.size + settings.VIDEO_ANALYSIS_PIPELINE_WORK_FREE_BYTES
    if shutil.disk_usage(files.root).free < needed:
        logger.warning("Skipping repackaging of %s: not enough disk", relative)
        return None
    with tempfile.TemporaryDirectory(prefix=".repackage-", dir=files.root) as work:
        source, target = Path(work) / "source.mp4", Path(work) / "target.mp4"
        with source.open("wb") as handle:
            client.download_fileobj(record.bucket, record.object_key, handle)
        with source.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != record.sha256:
                raise ValueError("Downloaded recording failed verification")
        repackage(source, target)
        verify_repackaged(source, target)
        with target.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            handle.seek(0)
            key = f"{files.prefix}/{uuid.uuid4().hex}/{relative}"
            files.client.upload_fileobj(
                handle,
                record.bucket,
                key,
                ExtraArgs={
                    "Metadata": {"sha256": checksum},
                    "ContentType": "video/mp4",
                },
            )
        size = target.stat().st_size
    if _checksum(files, record.bucket, key) != (size, checksum):
        files.client.delete_object(Bucket=record.bucket, Key=key)
        raise ValueError("Object storage verification failed")
    # Commit the replacement and its durable cleanup intent together. Keep the
    # expensive media and storage work outside this short transaction.
    try:
        with transaction.atomic():
            replaced = StoredFile.objects.filter(
                pk=record.pk, object_key=record.object_key
            ).update(object_key=key, sha256=checksum, size=size, source_mtime_ns=0)
            if replaced:
                repackaging.retire(files.workspace.pk, record.object_key)
    except Exception:
        delete_unreferenced(files, key)
        raise
    if not replaced:
        files.client.delete_object(Bucket=record.bucket, Key=key)
        return None
    return record.object_key


def delete_unreferenced(files: WorkspaceObjects, key: str) -> bool:
    """Delete a superseded media object once no stored file points at it.

    Returns:
        Whether the object was deleted.

    """
    bucket = settings.VIDEO_ANALYSIS_MEDIA_BUCKET
    if (
        not key.startswith(files.prefix + "/")
        or StoredFile.objects.filter(bucket=bucket, object_key=key).exists()
    ):
        return False
    files.client.delete_object(Bucket=bucket, Key=key)
    return True
