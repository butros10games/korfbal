"""Regression tests for project configuration and startup exports."""

from __future__ import annotations

from django.conf import settings
import pytest

from korfbal import celery_app
from korfbal.celery import app
from korfbal.settings_test import (
    _postgres_test_database_name,
    _sqlite_test_database_name,
)


def test_api_authentication_defaults_are_secure_and_ordered() -> None:
    """JWT/session/basic auth should all inherit an authenticated-by-default API."""
    assert settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"] == (
        "korfbal.authentication.JwtBearerAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    )
    assert settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] == (
        "rest_framework.permissions.IsAuthenticated",
    )


def test_test_settings_isolate_external_runtime_dependencies() -> None:
    """The suite must not silently depend on Redis, S3, or a live Celery worker."""
    assert settings.CACHES["default"]["BACKEND"] == (
        "django.core.cache.backends.locmem.LocMemCache"
    )
    assert settings.CHANNEL_LAYERS["default"]["BACKEND"] == (
        "channels.layers.InMemoryChannelLayer"
    )
    assert settings.STORAGES["default"]["BACKEND"] == (
        "django.core.files.storage.FileSystemStorage"
    )
    assert settings.CELERY_BROKER_URL == "memory://"
    assert settings.CELERY_RESULT_BACKEND == "cache+memory://"
    assert settings.CELERY_TASK_ALWAYS_EAGER is True


def test_parallel_test_lanes_use_distinct_database_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent pytest processes must never share an SQLite database file."""
    monkeypatch.delenv("KORFBAL_TEST_DB_LANE", raising=False)
    assert _sqlite_test_database_name() == "test.sqlite3"

    monkeypatch.setenv("KORFBAL_TEST_DB_LANE", "migrations")
    assert _sqlite_test_database_name() == "test-migrations.sqlite3"
    assert _postgres_test_database_name("korfbal") == "test_korfbal_migrations"


def test_package_exports_the_configured_celery_application() -> None:
    """Shared tasks must bind to the Django-configured project application."""
    assert celery_app is app
    assert app.main == "korfbal"
    assert app.conf.beat_schedule == {}
