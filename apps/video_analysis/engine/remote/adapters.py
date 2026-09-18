"""Outbound Runpod and S3 adapters; credentials remain on the controller."""

from __future__ import annotations

from http import HTTPStatus
import importlib
import json
from pathlib import Path

from .transport import request


class ProviderHTTPError(RuntimeError):
    """Retain a safe status code without a provider body or credentials."""

    def __init__(self, method: str, status: int) -> None:
        """Record only the operation and numeric HTTP status."""
        self.status = status
        super().__init__(f"Runpod {method} returned HTTP {status}")


class Runpod:
    """Minimal non-retrying REST adapter; reconcile ambiguous creates by name."""

    def __init__(self, api_key: str) -> None:
        """Keep the account key only in controller memory."""
        self.api_key = api_key

    def request(
        self, method: str, path: str, payload: dict | None = None
    ) -> dict | list | None:
        """Call Runpod without exposing response bodies or credentials in errors.

        Raises:
            ProviderHTTPError: If Runpod rejects the request.

        """
        with request(
            "https://rest.runpod.io/v1/" + path,
            method,
            json.dumps(payload).encode() if payload is not None else None,
            {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
        ) as response:
            if method == "DELETE" and response.status == HTTPStatus.NOT_FOUND:
                return None
            if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
                raise ProviderHTTPError(method, response.status)
            content = response.read()
            return json.loads(content) if content else None

    def list_pods(self) -> list[dict]:
        """Read provider state before making lifecycle decisions."""
        result = self.request("GET", "pods")
        assert isinstance(result, list)
        return result

    def create(self, payload: dict) -> dict:
        """Issue exactly one create request; do not automatically retry."""
        result = self.request("POST", "pods", payload)
        assert isinstance(result, dict)
        return result

    def delete(self, pod_id: str) -> None:
        """Delete a known owned pod; already absent is success.

        Raises:
            ValueError: If a provider ID contains path characters.

        """
        if not pod_id.isalnum():
            raise ValueError("Invalid Runpod ID")
        self.request("DELETE", "pods/" + pod_id)


class S3Artifacts:
    """Private artifacts with expiring object-specific worker URLs."""

    def __init__(self, bucket: str, endpoint: str | None = None) -> None:
        """Use the standard AWS credential chain on the controller only."""
        boto3 = importlib.import_module("boto3")
        config = importlib.import_module("botocore.config")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            config=config.Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 2},
            ),
        )
        self.bucket = bucket
        errors = importlib.import_module("botocore.exceptions")
        self.errors = (
            errors.BotoCoreError,
            errors.ClientError,
            importlib.import_module("boto3.exceptions").Boto3Error,
        )

    def upload(self, key: str, path: Path) -> None:
        """Store a kit before allocating paid compute.

        Raises:
            RuntimeError: If object storage rejects the transfer.

        """
        try:
            self.client.upload_file(str(path), self.bucket, key)
        except self.errors as error:
            raise RuntimeError(type(error).__name__) from None

    def url(self, key: str, operation: str, expires: int) -> str:
        """Grant one GET or PUT operation for a job-specific object.

        Raises:
            RuntimeError: If signing fails.

        """
        try:
            return self.client.generate_presigned_url(
                operation, Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
            )
        except self.errors as error:
            raise RuntimeError(type(error).__name__) from None

    def read(self, key: str, limit: int) -> bytes | None:
        """Read a bounded artifact; missing differs from permission/network failure.

        Raises:
            ValueError: If an artifact exceeds the download limit.
            RuntimeError: If object storage rejects the read.

        """
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except self.errors as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                return None
            raise RuntimeError(type(error).__name__) from None
        with response["Body"] as body:
            if response["ContentLength"] > limit:
                raise ValueError("Remote artifact exceeds its size limit")
            content = body.read(limit + 1)
        if len(content) > limit:
            raise ValueError("Remote artifact exceeds its size limit")
        return content
