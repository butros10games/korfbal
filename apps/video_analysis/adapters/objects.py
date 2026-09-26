"""Private S3 objects with immutable content keys and a verified local cache."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlsplit
import uuid

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from django.conf import settings
from django.db.models import Q, QuerySet

from apps.video_analysis.adapters.object_video import (
    object_url,
    probe_object,
    upload_video,
)
from apps.video_analysis.engine.storage_workspace import (
    STORAGE_PROTOCOL,
    reserve_space,
    storage_lease,
)
from apps.video_analysis.models import StoredFile, Workspace


logger = logging.getLogger(__name__)


class PublicationRaceError(ValueError):
    """Another process rewrote a file while it was being published."""


def workspace_root(workspace: Workspace) -> Path:
    """Use the immutable owner/workspace identity for cache and object scope."""
    return (
        Path(settings.VIDEO_ANALYSIS_ROOT).resolve()
        / str(workspace.owner_id)
        / str(workspace.pk)
    )


def object_client() -> BaseClient:
    """Reuse the deployment's private media connection without exposing credentials."""
    return boto3.client(
        "s3",
        endpoint_url=settings.KORFBAL_MEDIA_S3_ENDPOINT_URL,
        aws_access_key_id=settings.KORFBAL_MEDIA_S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.KORFBAL_MEDIA_S3_SECRET_ACCESS_KEY,
        region_name=settings.KORFBAL_MEDIA_S3_REGION_NAME,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 2},
            s3={"addressing_style": settings.KORFBAL_MEDIA_S3_ADDRESSING_STYLE},
        ),
    )


def legacy_object_client() -> BaseClient:
    """Read indexed MinIO objects until their verified Hetzner copy is recorded."""
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


class WorkspaceObjects:
    """Index private immutable payloads in Django; never treat cache as the backup."""

    def __init__(self, workspace: Workspace, client: BaseClient | None = None) -> None:
        """Bind one server-selected workspace and private S3 client."""
        self.workspace = workspace
        self.root = workspace_root(workspace)
        self.client = client if client is not None else object_client()
        self._legacy_client: BaseClient | None = None
        self.prefix = f"video-analysis/{workspace.owner_id}/{workspace.pk}"

    def read_client(self, record: StoredFile) -> BaseClient:
        """Select the provider recorded for this object during migration."""
        if record.bucket in {
            settings.VIDEO_ANALYSIS_MEDIA_BUCKET,
            settings.VIDEO_ANALYSIS_ARTIFACT_BUCKET,
        }:
            return self.client
        if self._legacy_client is None:
            self._legacy_client = legacy_object_client()
        return self._legacy_client

    def path(self, relative: str) -> Path:
        """Validate canonical relative paths before any local or remote operation.

        Raises:
            ValueError: The path escapes its workspace.

        """
        path = Path(relative)
        target = (self.root / path).resolve()
        if (
            path.is_absolute()
            or ".." in path.parts
            or not relative
            or not target.is_relative_to(self.root)
            or target == self.root
        ):
            raise ValueError("Invalid workspace file path")
        return target

    def _record(self, relative: str) -> StoredFile:
        """Resolve only this workspace's durable object mapping."""
        self.path(relative)
        return StoredFile.objects.get(workspace=self.workspace, relative_path=relative)

    def import_video(self, relative: str, chunks: Iterable[bytes]) -> dict:
        """Stream, validate and index private media before publishing a recording.

        Raises:
            ValueError: The recording format is unsupported.

        """
        self.path(relative)
        suffix = Path(relative).suffix.lower()
        if suffix not in {".mp4", ".webm"}:
            raise ValueError("Unsupported recording format")
        existing = StoredFile.objects.filter(
            workspace=self.workspace, relative_path=relative
        ).first()
        if existing:
            metadata = probe_object(
                self.read_client(existing),
                existing.bucket,
                existing.object_key,
                existing.size,
                suffix,
            )
            return {**metadata, "video_sha256": existing.sha256}
        bucket = settings.VIDEO_ANALYSIS_MEDIA_BUCKET
        key = f"{self.prefix}/{uuid.uuid4().hex}/{relative}"
        size, checksum, metadata = upload_video(
            self.client, bucket, key, chunks, suffix
        )
        try:
            StoredFile.objects.create(
                workspace=self.workspace,
                relative_path=relative,
                bucket=bucket,
                object_key=key,
                size=size,
                sha256=checksum,
                source_mtime_ns=0,
            )
        except Exception:
            self.client.delete_object(Bucket=bucket, Key=key)
            raise
        return {**metadata, "video_sha256": checksum}

    def upload(
        self, relative: str, *, media: bool = False, verify: bool = False
    ) -> StoredFile:
        """Publish an immutable payload, verifying its bytes before indexing it.

        Raises:
            ValueError: Media changed.
            PublicationRaceError: Another process rewrote the file meanwhile.

        """
        path = self.path(relative)
        current = StoredFile.objects.filter(
            workspace=self.workspace, relative_path=relative
        ).first()
        stat = path.stat()
        if (
            current
            and current.size == stat.st_size
            and current.source_mtime_ns == stat.st_mtime_ns
            and not verify
        ):
            return current
        with path.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            if media and current and current.sha256 != checksum:
                raise ValueError("Registered media is immutable; use a new filename")
            bucket = (
                settings.VIDEO_ANALYSIS_MEDIA_BUCKET
                if media
                else settings.VIDEO_ANALYSIS_ARTIFACT_BUCKET
            )
            key = f"{self.prefix}/{checksum}/{relative}"
            if (
                not current
                or current.sha256 != checksum
                or current.bucket != bucket
                or current.object_key != key
            ):
                handle.seek(0)
                self.client.upload_fileobj(
                    handle,
                    bucket,
                    key,
                    ExtraArgs={
                        "Metadata": {"sha256": checksum},
                        "ContentType": "application/octet-stream",
                    },
                )
            else:
                key = current.object_key
            self._verify(bucket, key, checksum, stat.st_size)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise PublicationRaceError(
                    "File changed during publication; retry after the writer finishes"
                )
        record, _ = StoredFile.objects.update_or_create(
            workspace=self.workspace,
            relative_path=relative,
            defaults={
                "bucket": bucket,
                "object_key": key,
                "sha256": checksum,
                "size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            },
        )
        return record

    def _verify(self, bucket: str, key: str, checksum: str, size: int) -> None:
        """Hash bytes read back from MinIO, including multipart uploads.

        Raises:
            ValueError: Remote bytes do not match the expected checksum.

        """
        response = self.client.get_object(Bucket=bucket, Key=key)
        with response["Body"] as body:
            digest = hashlib.sha256()
            count = 0
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                digest.update(chunk)
                count += len(chunk)
        if count != size or digest.hexdigest() != checksum:
            raise ValueError("Object storage verification failed")

    def evict_bulk(self) -> None:
        """Remove only verified published bulk bytes while holding storage_lease.

        JSON remains a small control-plane mirror for the native controller.
        Unpublished or modified outputs remain available for publication recovery.
        """
        heartbeat = self.root / "vision/remote/controller.json"
        modern_controller = (
            heartbeat.exists()
            and json.loads(heartbeat.read_text()).get("storage_protocol")
            == STORAGE_PROTOCOL
        )
        for record in StoredFile.objects.filter(workspace=self.workspace).iterator():
            if record.relative_path.endswith(".json"):
                continue
            path = self.path(record.relative_path)
            if not path.is_file():
                continue
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (
                record.size,
                record.source_mtime_ns,
            ):
                continue
            if record.relative_path.endswith("/kit.zip"):
                marker = path.with_name("job.json")
                if marker.exists():
                    job = json.loads(marker.read_text())
                    if (not job.get("kit_object") or not modern_controller) and job.get(
                        "status"
                    ) not in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        # Older requests still require their local kit.
                        continue
            path.unlink()

    def _reserve(self, additional: int) -> None:
        """Check the configured workspace limit before downloading or producing data."""
        reserve_space(
            self.root,
            additional,
            settings.VIDEO_ANALYSIS_WORKSPACE_MAX_BYTES,
            settings.VIDEO_ANALYSIS_PIPELINE_WORK_FREE_BYTES,
        )

    def hydrate_prefix(self, relative: str) -> None:
        """Restore only one selected frozen dataset or trained model."""
        self.path(relative)
        records = list(
            StoredFile.objects.filter(
                workspace=self.workspace,
                relative_path__startswith=relative.rstrip("/") + "/",
            )
        )
        missing = [
            record
            for record in records
            if not self.path(record.relative_path).is_file()
        ]
        self._reserve(sum(record.size for record in missing))
        for record in records:
            self.cache_media(record.relative_path)

    def artifact_location(self, relative: str) -> dict:
        """Return a private object reference, never credentials or a public URL."""
        record = self._record(relative)
        return {
            "bucket": record.bucket,
            "key": record.object_key,
            "size": record.size,
            "sha256": record.sha256,
        }

    def cache_media(self, relative: str) -> Path:
        """Restore missing worker inputs atomically and verify the download.

        Raises:
            ValueError: Downloaded bytes do not match the indexed checksum.

        """
        record = self._record(relative)
        path = self.path(relative)
        if (
            path.is_file()
            and path.stat().st_size == record.size
            and path.stat().st_mtime_ns == record.source_mtime_ns
        ):
            return path
        self._reserve(record.size)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            storage_lease(self.root, name=".cache.lock"),
            tempfile.NamedTemporaryFile(
                prefix=".cache-", dir=path.parent, delete=False
            ) as handle,
        ):
            temporary = Path(handle.name)
            try:
                self.read_client(record).download_fileobj(
                    record.bucket, record.object_key, handle
                )
                handle.flush()
                with temporary.open("rb") as content:
                    checksum = hashlib.file_digest(content, "sha256").hexdigest()
                if temporary.stat().st_size != record.size or checksum != record.sha256:
                    raise ValueError("Cached media verification failed")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        StoredFile.objects.filter(pk=record.pk).update(
            source_mtime_ns=path.stat().st_mtime_ns
        )
        return path

    @contextmanager
    def video_source(self, relative: str) -> Iterator[str]:
        """Serve worker seeks from private storage without persisting a recording.

        Yields:
            A loopback-only URL for one registered object.

        """
        record = self._record(relative)
        # Resolve the ORM record in the caller, not in the range server threads.
        client = self.read_client(record)

        def read(start: int, end: int) -> Iterator[bytes]:
            response = client.get_object(
                Bucket=record.bucket,
                Key=record.object_key,
                Range=f"bytes={start}-{end}",
            )
            with response["Body"] as body:
                yield from iter(lambda: body.read(64 * 1024), b"")

        with object_url(record.size, read) as source:
            yield source

    def media_url(self, relative: str) -> str | None:
        """Grant a short-lived GET of one already authorized immutable object."""
        record = self._record(relative)
        if record.bucket not in {
            settings.VIDEO_ANALYSIS_MEDIA_BUCKET,
            settings.VIDEO_ANALYSIS_ARTIFACT_BUCKET,
        }:
            return None
        url = self.read_client(record).generate_presigned_url(
            "get_object",
            Params={
                "Bucket": record.bucket,
                "Key": record.object_key,
                "ResponseContentType": mimetypes.guess_type(relative)[0]
                or "application/octet-stream",
            },
            ExpiresIn=3600,
        )
        return url if urlsplit(url).scheme == "https" else None

    def size(self, relative: str) -> int:
        """Serve metadata without downloading the full match video."""
        return self._record(relative).size

    def chunks(self, relative: str, start: int, end: int) -> Iterator[bytes]:
        """Stream a requested range from the indexed private provider after MFA checks.

        Yields:
            Bounded response chunks.

        """
        record = self._record(relative)
        response = self.read_client(record).get_object(
            Bucket=record.bucket, Key=record.object_key, Range=f"bytes={start}-{end}"
        )
        with response["Body"] as body:
            yield from iter(lambda: body.read(1024 * 1024), b"")

    def publish_media(self, relative: str) -> None:
        """Publish one image using immutable owner-scoped media storage."""
        self.upload(relative, media=True)

    def publish_review(self, data: dict[str, Any]) -> None:
        """Upload new registered media and retain a durable export of each revision."""
        registered = {f["image"] for m in data["matches"] for f in m["frames"]}
        registered.update(m["video"] for m in data["matches"] if m.get("video"))
        known = set(
            StoredFile.objects.filter(
                workspace=self.workspace, relative_path__in=registered
            ).values_list("relative_path", flat=True)
        )
        for relative in sorted(registered - known):
            self.upload(relative, media=True)
        relative = f"reviews/revision-{data['revision']:012d}.json"
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, allow_nan=False, sort_keys=True).encode()
        # Revisions can be retried after a rolled-back DB transaction. Content-addressed
        # keys preserve both attempts while the mapping follows the committed revision.
        path.write_bytes(payload)
        self.upload(relative)

    def hydrate_artifacts(self, *, metadata_only: bool = False) -> None:
        """Restore missing artifacts, preserving an active controller's local writes."""
        records = StoredFile.objects.filter(
            workspace=self.workspace, relative_path__startswith="vision/"
        )
        if metadata_only:
            records = records.filter(relative_path__endswith=".json").exclude(
                relative_path__startswith="vision/clips/"
            )
        for record in records:
            if not self.path(record.relative_path).exists():
                self.cache_media(record.relative_path)

    def sync_artifacts(self) -> None:
        """Publish stable files; atomic producer writes permit safe periodic retries.

        A file another process (such as the training controller) is still
        writing is left for the next periodic pass instead of failing the job
        whose own outputs were already published.
        """
        for path in sorted((self.root / "vision").rglob("*")):
            relative = path.relative_to(self.root)
            if (
                path.is_file()
                and not any(part.startswith(".") for part in relative.parts)
                and path.suffix not in {".tmp", ".lock"}
            ):
                try:
                    self.upload(relative.as_posix())
                except (PublicationRaceError, FileNotFoundError):
                    logger.info("Deferred publication of %s to the next sync", relative)

    def publish_artifact(self, relative: str) -> None:
        """Publish the changed metadata without scanning unrelated artifacts."""
        self.upload(relative)

    def purge_upload(self, upload_id: uuid.UUID) -> None:
        """Delete indexed temporary chunks under one immutable workspace owner."""
        relative_prefix = f"uploads/{uuid.UUID(str(upload_id))}/"
        self._purge(
            StoredFile.objects.filter(
                workspace=self.workspace, relative_path__startswith=relative_prefix
            )
        )

    def purge_clip(self, run_id: uuid.UUID) -> None:
        """Delete one clip run's indexed artifacts, including replay section parts."""
        name = str(uuid.UUID(str(run_id)))
        self._purge(
            StoredFile.objects.filter(workspace=self.workspace).filter(
                Q(relative_path__startswith=f"vision/clips/{name}/")
                | Q(relative_path__startswith=f"vision/clips/{name}-part-")
            )
        )

    def _purge(self, rows: QuerySet[StoredFile]) -> None:
        """Delete indexed objects, their cache files and their mappings.

        Raises:
            ValueError: An indexed object escapes its workspace.

        """
        for row in rows.iterator(chunk_size=100):
            if not (
                row.object_key.startswith(self.prefix + "/")
                and row.object_key.endswith("/" + row.relative_path)
            ):
                raise ValueError("Stored object is outside the workspace")
            self.read_client(row).delete_object(Bucket=row.bucket, Key=row.object_key)
            self.path(row.relative_path).unlink(missing_ok=True)
            row.delete()
