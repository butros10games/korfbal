"""Test settings for the korfbal project.

Mirrors the production configuration but replaces external service dependencies
(Postgres, Redis/Valkey, S3, Celery) with in-memory or local backends so the test
suite can run without additional infrastructure.
"""

from __future__ import annotations

import os

from .settings import *  # noqa: F403
from .settings.env import BASE_DIR
from .settings.services import DATABASES


# Keep ruff happy with explicit bindings for star-imported settings.
# (We intentionally override these below.)
STORAGES = globals().get("STORAGES", {})
STATICFILES_STORAGE = globals().get("STATICFILES_STORAGE", "")


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
# Performance knobs (tests)
# ---------------------------------------------------------------------------
# The Team page stats tests assert that we recompute and persist match impact
# rows when outdated. Local developer .env files may disable recompute for
# production-like behavior; tests must force it on.
KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE = True
KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT = 25


# ---------------------------------------------------------------------------
# Database fallback for tests
# ---------------------------------------------------------------------------
if os.getenv("DJANGO_TEST_USE_POSTGRES", "").lower() not in {"1", "true", "yes", "on"}:
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(BASE_DIR / "test.sqlite3"),
    }


# ---------------------------------------------------------------------------
# Storage override (avoid S3/MinIO dependencies during tests)
# ---------------------------------------------------------------------------
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": BASE_DIR / "media_test"},
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


# ---------------------------------------------------------------------------
# Celery: use in-memory broker/backend for tests
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = True
