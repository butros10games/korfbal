"""Static/media storage configuration (MinIO/S3)."""

from __future__ import annotations

from pathlib import Path

from .env import BASE_DIR, env, env_bool, env_int


AWS_S3_ENDPOINT_URL = env("MINIO_URL", "http://kwt-minio:9000")
AWS_S3_CUSTOM_DOMAIN = env("MINIO_PUBLIC_DOMAIN", "localhost")
AWS_STATIC_CUSTOM_DOMAIN = f"static.{AWS_S3_CUSTOM_DOMAIN}"
AWS_MEDIA_CUSTOM_DOMAIN = f"media.{AWS_S3_CUSTOM_DOMAIN}"
AWS_ACCESS_KEY_ID = env("MINIO_ACCESS_KEY", "minioadmin")
AWS_SECRET_ACCESS_KEY = env("MINIO_SECRET_KEY", "minioadmin")
AWS_STORAGE_BUCKET_NAME = env("STATIC_BUCKET", "static")
AWS_MEDIA_BUCKET_NAME = env("MEDIA_BUCKET", "media")
AWS_QUERYSTRING_AUTH = env_bool("AWS_QUERYSTRING_AUTH", True)
AWS_QUERYSTRING_EXPIRE = env_int("AWS_QUERYSTRING_EXPIRE", 3600)
AWS_S3_CONFIG = {"retries": {"max_attempts": 5, "mode": "standard"}}

STATIC_URL = env("STATIC_URL", f"{AWS_STATIC_CUSTOM_DOMAIN}/")
MEDIA_URL = env("MEDIA_URL", f"{AWS_MEDIA_CUSTOM_DOMAIN}/")
STATIC_ROOT = Path(env("STATIC_ROOT", str(BASE_DIR / "static")))
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "media")))
# No extra static dirs: old Django frontend assets lived in static_workfile/.
STATICFILES_DIRS: list[Path] = []

S3_STORAGE_BACKEND = "storages.backends.s3boto3.S3Boto3Storage"
DEFAULT_STORAGE_BACKEND = env("DEFAULT_STORAGE_BACKEND", S3_STORAGE_BACKEND)
STATICFILES_STORAGE_BACKEND = env("STATICFILES_STORAGE", S3_STORAGE_BACKEND)

STORAGES = {
    "default": {
        "BACKEND": DEFAULT_STORAGE_BACKEND,
        "OPTIONS": {
            "bucket_name": AWS_MEDIA_BUCKET_NAME,
            # Personal data (profile pictures) should not be world-readable.
            "default_acl": "private",
            "querystring_auth": AWS_QUERYSTRING_AUTH,
            "querystring_expire": AWS_QUERYSTRING_EXPIRE,
            "custom_domain": AWS_MEDIA_CUSTOM_DOMAIN,
        },
    },
    "staticfiles": {
        "BACKEND": STATICFILES_STORAGE_BACKEND,
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "default_acl": "public-read",
            "querystring_auth": False,
            "custom_domain": AWS_STATIC_CUSTOM_DOMAIN,
        },
    },
}
