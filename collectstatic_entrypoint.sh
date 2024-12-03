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

# Configure mc for MinIO
mc alias set "$MINIO_ALIAS" "$MINIO_URL" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"

# Check and create buckets if they don't exist
for BUCKET in "$STATIC_BUCKET" "$MEDIA_BUCKET"; do
    if mc ls "$MINIO_ALIAS/$BUCKET" > /dev/null 2>&1; then
        echo "Bucket $BUCKET already exists."
    else
        echo "Bucket $BUCKET does not exist. Creating..."
        mc mb "$MINIO_ALIAS/$BUCKET"
    fi
done

# Run collectstatic
echo "Running collectstatic..."
python manage.py webpack_collectstatic

echo "Collectstatic complete. Exiting."
