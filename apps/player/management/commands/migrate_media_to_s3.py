"""Copy existing private app media to the configured S3 bucket without deleting it."""

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser


MAX_WORKERS = 16


def _client(
    endpoint: str, key: str, secret: str, region: str, style: str
) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": style}),
    )


def _digest(body: BinaryIO, output: BinaryIO | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: body.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
        if output is not None:
            output.write(chunk)
    return digest.hexdigest(), size


def _copy_one(
    source: BaseClient,
    target: BaseClient,
    source_bucket: str,
    target_bucket: str,
    item: dict[str, Any],
) -> int:
    """Copy or verify one key without replacing different target bytes.

    Returns:
        Verified object size in bytes.

    Raises:
        ClientError: S3 refuses a read or write.
        CommandError: The source changed or the destination differs.

    """
    key = item["Key"]
    with SpooledTemporaryFile(max_size=16 * 1024 * 1024) as temporary:
        response = source.get_object(Bucket=source_bucket, Key=key)
        with response["Body"] as body:
            checksum, size = _digest(body, temporary)
        if size != item["Size"]:
            raise CommandError(f"Source size changed during copy: {key}")
        try:
            response = target.get_object(Bucket=target_bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                "NoSuchKey",
                "404",
            }:
                raise
            temporary.seek(0)
            target.upload_fileobj(temporary, target_bucket, key)
            response = target.get_object(Bucket=target_bucket, Key=key)
        with response["Body"] as body:
            target_checksum, target_size = _digest(body)
        if (target_checksum, target_size) != (checksum, size):
            raise CommandError(f"Target differs from source: {key}")
    return size


def copy_media(
    source: BaseClient,
    target: BaseClient,
    source_bucket: str,
    target_bucket: str,
    *,
    workers: int = 8,
) -> tuple[int, int]:
    """Copy missing keys concurrently and verify every target byte.

    Existing target keys must match exactly; this never overwrites or deletes data.

    Raises:
        ValueError: Worker count is outside the bounded range.

    """
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError("workers must be between 1 and 16")
    count = total_bytes = 0
    pages = source.get_paginator("list_objects_v2").paginate(Bucket=source_bucket)
    pending: deque[Future[int]] = deque()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for page in pages:
            for item in page.get("Contents", []):
                pending.append(
                    executor.submit(
                        _copy_one, source, target, source_bucket, target_bucket, item
                    )
                )
                if len(pending) >= workers * 4:
                    total_bytes += pending.popleft().result()
                    count += 1
        while pending:
            total_bytes += pending.popleft().result()
            count += 1
    return count, total_bytes


class Command(BaseCommand):
    """Migrate the existing MinIO media bucket before changing app storage."""

    help = "Copy and verify private app media from MinIO to the configured S3 bucket."

    def add_arguments(self, parser: CommandParser) -> None:
        """Require the explicit source bucket so an old bucket cannot be guessed."""
        parser.add_argument("--source-bucket", required=True)
        parser.add_argument("--workers", type=int, default=8)

    def handle(self, *args: object, **options: object) -> None:
        """Copy old media into the active private-media bucket.

        Raises:
            CommandError: Source and target are identical.

        """
        if (
            settings.AWS_S3_ENDPOINT_URL == settings.KORFBAL_MEDIA_S3_ENDPOINT_URL
            and options["source_bucket"] == settings.AWS_MEDIA_BUCKET_NAME
        ):
            raise CommandError("Source and target must be distinct buckets")
        source = _client(
            settings.AWS_S3_ENDPOINT_URL,
            settings.AWS_ACCESS_KEY_ID,
            settings.AWS_SECRET_ACCESS_KEY,
            "us-east-1",
            "path",
        )
        target = _client(
            settings.KORFBAL_MEDIA_S3_ENDPOINT_URL,
            settings.KORFBAL_MEDIA_S3_ACCESS_KEY_ID,
            settings.KORFBAL_MEDIA_S3_SECRET_ACCESS_KEY,
            settings.KORFBAL_MEDIA_S3_REGION_NAME,
            settings.KORFBAL_MEDIA_S3_ADDRESSING_STYLE,
        )
        count, total_bytes = copy_media(
            source,
            target,
            str(options["source_bucket"]),
            settings.AWS_MEDIA_BUCKET_NAME,
            workers=int(options["workers"]),
        )
        self.stdout.write(f"Verified {count} objects ({total_bytes} bytes).")
