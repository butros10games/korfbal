"""Storage policy and canary contracts using AWS SDK request validation."""

from http import HTTPStatus
import json
from unittest.mock import patch
from uuid import UUID, uuid4

import boto3
from botocore.stub import Stubber
import pytest

from apps.player.adapters.outbound.media_privacy import (
    probe_media_privacy,
    verify_media_policy,
)


BUCKET = "synthetic-media"
PUBLIC = {
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": f"arn:aws:s3:::{BUCKET}/*",
}
PRIVATE = {**PUBLIC, "Principal": {"AWS": "arn:aws:iam::123456789012:root"}}


def test_public_policy_fails_without_mutation() -> None:
    """Verification cannot quietly accept a public bucket."""
    client = boto3.client(
        "s3", aws_access_key_id="synthetic", aws_secret_access_key=uuid4().hex
    )
    with Stubber(client) as stub:
        stub.add_response(
            "get_bucket_policy",
            {"Policy": json.dumps({"Statement": [PUBLIC]})},
            {"Bucket": BUCKET},
        )
        with pytest.raises(RuntimeError, match="anonymous"):
            verify_media_policy(client, BUCKET, repair=False)
        stub.assert_no_pending_responses()


def test_repair_preserves_private_grants_and_checks_result() -> None:
    """Remove public grants without dropping an existing authenticated grant."""
    client = boto3.client(
        "s3", aws_access_key_id="synthetic", aws_secret_access_key=uuid4().hex
    )
    private = {"Statement": [PRIVATE]}
    with Stubber(client) as stub:
        stub.add_response(
            "get_bucket_policy",
            {"Policy": json.dumps({"Statement": [PUBLIC, PRIVATE]})},
            {"Bucket": BUCKET},
        )
        stub.add_response(
            "put_bucket_policy", {}, {"Bucket": BUCKET, "Policy": json.dumps(private)}
        )
        stub.add_response(
            "get_bucket_policy", {"Policy": json.dumps(private)}, {"Bucket": BUCKET}
        )
        verify_media_policy(client, BUCKET, repair=True)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "anonymous_status", [HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND]
)
def test_probe_always_cleans_up_and_accepts_only_explicit_denial(
    anonymous_status: HTTPStatus,
) -> None:
    """Success and inconclusive errors fail; every path removes only its canary."""
    client = boto3.client(
        "s3", aws_access_key_id="synthetic", aws_secret_access_key=uuid4().hex
    )
    anonymous = boto3.client(
        "s3", aws_access_key_id="synthetic", aws_secret_access_key=uuid4().hex
    )
    identity = UUID(int=0)
    target = {"Bucket": BUCKET, "Key": f"security-probes/{identity.hex}"}
    with (
        Stubber(client) as signed,
        Stubber(anonymous) as unsigned,
        patch(
            "apps.player.adapters.outbound.media_privacy.uuid4", return_value=identity
        ),
    ):
        signed.add_response(
            "put_object",
            {},
            {**target, "Body": b"korfbal-private-media-probe", "ACL": "private"},
        )
        signed.add_response("head_object", {}, target)
        signed.add_response("delete_object", {}, target)
        if anonymous_status == HTTPStatus.OK:
            unsigned.add_response("head_object", {}, target)
        else:
            unsigned.add_client_error(
                "head_object",
                service_error_code=str(anonymous_status.value),
                http_status_code=anonymous_status.value,
                expected_params=target,
            )
        if anonymous_status == HTTPStatus.FORBIDDEN:
            probe_media_privacy(client, anonymous, BUCKET)
        else:
            with pytest.raises(RuntimeError):
                probe_media_privacy(client, anonymous, BUCKET)
        signed.assert_no_pending_responses()
        unsigned.assert_no_pending_responses()
