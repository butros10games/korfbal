"""Copy and verify private video files into MinIO without deleting originals."""

from argparse import ArgumentParser
from http import HTTPStatus
import json

from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.models import StoredFile, Workspace


def provision(files: WorkspaceObjects) -> None:
    """Create private versioned buckets without changing existing public policies.

    Raises:
        ClientError: MinIO rejects bucket configuration.
        CommandError: A bucket has an existing access policy.

    """
    for bucket in (
        settings.VIDEO_ANALYSIS_MEDIA_BUCKET,
        settings.VIDEO_ANALYSIS_ARTIFACT_BUCKET,
        "korfbal-video-jobs",
    ):
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
        """Populate verified mappings before enabling MinIO.

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
