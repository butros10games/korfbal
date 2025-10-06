"""Test settings for the korfbal project.

Mirrors the production configuration but replaces external service
dependencies (Postgres, Redis/Valkey, S3, Celery) with in-memory or local
backends so the test suite can run without additional infrastructure.
"""

from __future__ import annotations

import os

from .settings import *  # noqa: F403  # NOSONAR


# In-memory cache (avoid Valkey/Redis during tests)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "korfbal-test-cache",
    }
}

# Ensure sessions use the local cache backend defined above
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# Test indicators / overrides
TESTING = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# ---------------------------------------------------------------------------
# Database fallback for tests
# ---------------------------------------------------------------------------
if os.getenv("DJANGO_TEST_USE_POSTGRES", "").lower() not in {"1", "true", "yes", "on"}:
    DATABASES["default"] = {  # noqa: F405
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(BASE_DIR / "test.sqlite3"),  # noqa: F405
    }


# ---------------------------------------------------------------------------
# Storage override (avoid S3/MinIO dependencies during tests)
# ---------------------------------------------------------------------------
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": BASE_DIR / "media_test"},  # noqa: F405
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


# ---------------------------------------------------------------------------
# Channels: force in-memory layer for tests (avoid Redis/Valkey dependency)
# ---------------------------------------------------------------------------
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


# ---------------------------------------------------------------------------
# Celery: use in-memory broker/backend for tests
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = True
