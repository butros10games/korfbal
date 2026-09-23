"""Copy and verify private video files into S3 without deleting originals."""

from argparse import ArgumentParser
import hashlib
from http import HTTPStatus
import json
from pathlib import Path
import tempfile

from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.models import StoredFile, Workspace


def restore_legacy_file(files: WorkspaceObjects, record: StoredFile) -> None:
    """Restore a missing old MinIO object before moving its indexed mapping.

    Raises:
        CommandError: The old object no longer matches its indexed hash and size.

    """
    path = files.path(record.relative_path)
    if path.is_file():
        return
    source = files.read_client(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            source.download_fileobj(record.bucket, record.object_key, handle)
            handle.flush()
            with temporary.open("rb") as content:
                checksum = hashlib.file_digest(content, "sha256").hexdigest()
            if temporary.stat().st_size != record.size or checksum != record.sha256:
                raise CommandError("Legacy video object failed checksum verification")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def provision(files: WorkspaceObjects) -> None:
    """Create private versioned buckets without changing existing public policies.

    Raises:
        ClientError: S3 rejects bucket configuration.
        CommandError: A bucket has an existing access policy.

    """
    buckets = [
        settings.VIDEO_ANALYSIS_MEDIA_BUCKET,
        settings.VIDEO_ANALYSIS_ARTIFACT_BUCKET,
    ]
    if settings.KORFBAL_MEDIA_S3_ENDPOINT_URL == settings.AWS_S3_ENDPOINT_URL:
        buckets.append("korfbal-video-jobs")
    for bucket in dict.fromkeys(buckets):
        try:
            files.client.head_bucket(Bucket=bucket)
        except ClientError as error:
            if (
                error.response["ResponseMetadata"]["HTTPStatusCode"]
                != HTTPStatus.NOT_FOUND
            ):
                raise
            files.client.create_bucket(Bucket=bucket)
        try:
            files.client.get_bucket_policy(Bucket=bucket)
        except ClientError as error:
            if error.response["Error"]["Code"] not in {
                "NoSuchBucketPolicy",
                "NoSuchPolicy",
            }:
                raise
        else:
            raise CommandError("Video buckets must have no public bucket policy")
        files.client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )


class Command(BaseCommand):
    """Idempotently publish current files and the latest authoritative reviews."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Configure explicit provisioning and full-byte verification."""
        parser.add_argument("--slug", default="main")
        parser.add_argument("--create-buckets", action="store_true")
        parser.add_argument("--verify", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Populate verified mappings before enabling object storage.

        Raises:
            CommandError: A workspace contains unsafe symlinks.

        """
        workspace = Workspace.objects.get(slug=options["slug"])
        files = WorkspaceObjects(workspace)
        if options["create_buckets"]:
            provision(files)
        store = DatabaseStore(workspace, files=files)
        data = store.read()
        media = {f["image"] for m in data["matches"] for f in m["frames"]}
        media.update(m["video"] for m in data["matches"] if m.get("video"))
        for record in StoredFile.objects.filter(workspace=workspace):
            target_bucket = (
                settings.VIDEO_ANALYSIS_MEDIA_BUCKET
                if record.relative_path in media
                else settings.VIDEO_ANALYSIS_ARTIFACT_BUCKET
            )
            if record.bucket != target_bucket or not record.object_key.startswith(
                files.prefix + "/"
            ):
                restore_legacy_file(files, record)
                files.upload(
                    record.relative_path,
                    media=record.relative_path in media,
                    verify=True,
                )
        for relative in sorted(media):
            files.upload(relative, media=True, verify=bool(options["verify"]))
        for path in sorted(store.root.rglob("*")):
            relative = path.relative_to(store.root)
            if path.is_symlink():
                raise CommandError("Refusing symlinks in workspace storage")
            if (
                not path.is_file()
                or any(part.startswith(".") for part in relative.parts)
                or path.suffix in {".lock", ".tmp"}
                or relative.as_posix() in media
            ):
                continue
            files.upload(relative.as_posix(), verify=bool(options["verify"]))
        with store.transaction():
            files.publish_review(store.read())
        records = StoredFile.objects.filter(workspace=workspace)
        self.stdout.write(
            json.dumps({
                "files": records.count(),
                "bytes": sum(records.values_list("size", flat=True)),
                "revision": store.read()["revision"],
                "verified": True,
                "local_files_retained": True,
            })
        )
