"""Private MinIO objects with immutable content keys and a verified local cache."""

from collections.abc import Iterator
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from django.conf import settings

from apps.video_analysis.models import StoredFile, Workspace


def workspace_root(workspace: Workspace) -> Path:
    """Use the immutable owner/workspace identity for cache and object scope."""
    return (
        Path(settings.VIDEO_ANALYSIS_ROOT).resolve()
        / str(workspace.owner_id)
        / str(workspace.pk)
    )


def object_client() -> BaseClient:
    """Reuse the deployment's private MinIO connection without exposing credentials."""
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
    )


class WorkspaceObjects:
    """Index private immutable payloads in Django; never treat cache as the backup."""

    def __init__(self, workspace: Workspace, client: BaseClient | None = None) -> None:
        """Bind one server-selected workspace and private S3 client."""
        self.workspace = workspace
        self.root = workspace_root(workspace)
        self.client = client if client is not None else object_client()
        self.prefix = f"{workspace.owner_id}/{workspace.pk}"

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

    def record(self, relative: str) -> StoredFile:
        """Resolve only this workspace's durable object mapping."""
        self.path(relative)
        return StoredFile.objects.get(workspace=self.workspace, relative_path=relative)

    def upload(
        self, relative: str, *, media: bool = False, verify: bool = False
    ) -> StoredFile:
        """Publish an immutable payload, verifying its bytes before indexing it.

        Raises:
            ValueError: Media changed or publication raced a writer.

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
            if not current or current.sha256 != checksum or current.bucket != bucket:
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
            self.verify(bucket, key, checksum, stat.st_size)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError(
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

    def verify(self, bucket: str, key: str, checksum: str, size: int) -> None:
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

    def cache_media(self, relative: str) -> Path:
        """Restore missing worker inputs atomically and verify the download.

        Raises:
            ValueError: Downloaded bytes do not match the indexed checksum.

        """
        record = self.record(relative)
        path = self.path(relative)
        if (
            path.is_file()
            and path.stat().st_size == record.size
            and path.stat().st_mtime_ns == record.source_mtime_ns
        ):
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                self.client.download_fileobj(record.bucket, record.object_key, handle)
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

    def size(self, relative: str) -> int:
        """Serve metadata without downloading the full match video."""
        return self.record(relative).size

    def chunks(self, relative: str, start: int, end: int) -> Iterator[bytes]:
        """Stream a requested range directly from private MinIO after MFA checks.

        Yields:
            Bounded response chunks.

        """
        record = self.record(relative)
        response = self.client.get_object(
            Bucket=record.bucket, Key=record.object_key, Range=f"bytes={start}-{end}"
        )
        with response["Body"] as body:
            yield from iter(lambda: body.read(1024 * 1024), b"")

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
        """Publish stable files; atomic producer writes permit safe periodic retries."""
        for path in sorted((self.root / "vision").rglob("*")):
            relative = path.relative_to(self.root)
            if (
                path.is_file()
                and not any(part.startswith(".") for part in relative.parts)
                and path.suffix not in {".tmp", ".lock"}
            ):
                self.upload(relative.as_posix())

    def publish_artifact(self, relative: str) -> None:
        """Publish the changed metadata without scanning unrelated artifacts."""
        self.upload(relative)
