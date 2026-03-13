#!/bin/bash

# Exit on any error
set -e

# Variables
MINIO_ALIAS="local"
MINIO_URL="${MINIO_URL:-http://kwt-minio:9000}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin}"
STATIC_BUCKET="${STATIC_BUCKET:-static}"
MEDIA_BUCKET="${MEDIA_BUCKET:-media}"

# Log configuration
echo "Configuring MinIO client..."
echo "MinIO URL: $MINIO_URL"
echo "MinIO Alias: $MINIO_ALIAS"
echo "Static Bucket: $STATIC_BUCKET"
echo "Media Bucket: $MEDIA_BUCKET"

# Check and create buckets if they don't exist
python - <<'PY'
import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

minio_url = os.environ.get("MINIO_URL", "http://kwt-minio:9000")
minio_access_key = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
minio_secret_key = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
buckets = [
    os.environ.get("STATIC_BUCKET", "static"),
    os.environ.get("MEDIA_BUCKET", "media"),
]

s3 = boto3.client(
    "s3",
    endpoint_url=minio_url,
    aws_access_key_id=minio_access_key,
    aws_secret_access_key=minio_secret_key,
    region_name="us-east-1",
    config=Config(signature_version="s3v4"),
)

for bucket in buckets:
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"Bucket {bucket} already exists.")
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = str(error.get("Code", ""))
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchBucket", "NotFound"} or status_code == 404:
            print(f"Bucket {bucket} does not exist. Creating...")
            s3.create_bucket(Bucket=bucket)
        else:
            raise
PY

# Run collectstatic
echo "Running collectstatic..."
python manage.py collectstatic --noinput

echo "Collectstatic complete. Exiting."
