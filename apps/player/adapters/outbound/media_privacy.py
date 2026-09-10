"""Verify and repair MinIO media policy without touching user objects."""

from http import HTTPStatus
import json
from typing import Any
from uuid import uuid4

import boto3
from botocore import UNSIGNED
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings


def _public_allow(statement: dict[str, Any]) -> bool:
    principal = statement.get("Principal")
    if isinstance(principal, dict):
        principal = principal.get("AWS")
    return statement.get("Effect") == "Allow" and (
        principal == "*"
        or (isinstance(principal, list) and "*" in principal)
        or "NotPrincipal" in statement
    )


def verify_media_policy(client: BaseClient, bucket: str, *, repair: bool) -> None:
    """Reject public allow statements, optionally removing only those statements.

    Raises:
        RuntimeError: The policy grants anonymous access and repair was not requested.
        ClientError: The storage policy could not be read or changed.

    """
    try:
        document = json.loads(client.get_bucket_policy(Bucket=bucket)["Policy"])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NoSuchBucketPolicy":
            return
        raise
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    private = [statement for statement in statements if not _public_allow(statement)]
    if len(private) == len(statements):
        return
    if not repair:
        raise RuntimeError("Media bucket allows anonymous access; repair its policy.")
    if private:
        document["Statement"] = private
        client.put_bucket_policy(Bucket=bucket, Policy=json.dumps(document))
    else:
        client.delete_bucket_policy(Bucket=bucket)
    verify_media_policy(client, bucket, repair=False)


def probe_media_privacy(client: BaseClient, anonymous: BaseClient, bucket: str) -> None:
    """Check anonymous denial using a new synthetic object, then always remove it.

    Raises:
        RuntimeError: Anonymous access succeeded or its failure was inconclusive.

    """
    key = f"security-probes/{uuid4().hex}"
    client.put_object(
        Bucket=bucket, Key=key, Body=b"korfbal-private-media-probe", ACL="private"
    )
    try:
        client.head_object(Bucket=bucket, Key=key)
        try:
            anonymous.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if (
                error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                != HTTPStatus.FORBIDDEN
            ):
                raise RuntimeError(
                    "Anonymous media denial was inconclusive."
                ) from error
        else:
            raise RuntimeError("Anonymous clients can access private media.")
    finally:
        client.delete_object(Bucket=bucket, Key=key)


def check_media_privacy(*, repair: bool, probe: bool) -> None:
    """Apply the MinIO deployment check to the configured media bucket.

    Raises:
        ValueError: Media and static files share a bucket.

    """
    if settings.AWS_MEDIA_BUCKET_NAME == settings.AWS_STORAGE_BUCKET_NAME:
        raise ValueError("Media and static files must use separate buckets.")
    client = boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=10),
    )
    verify_media_policy(client, settings.AWS_MEDIA_BUCKET_NAME, repair=repair)
    if probe:
        anonymous = boto3.client(
            "s3",
            endpoint_url=settings.AWS_S3_ENDPOINT_URL,
            config=Config(
                signature_version=UNSIGNED, connect_timeout=5, read_timeout=10
            ),
        )
        probe_media_privacy(client, anonymous, settings.AWS_MEDIA_BUCKET_NAME)
