"""Regression tests for security-sensitive Django settings."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess  # nosec B404
import sys

from django.conf import settings
from korfbal.settings import security
import pytest


PROJECT_DIR = Path(__file__).resolve().parents[3]
PRODUCTION_ENV = {
    "DJANGO_ENV": "production",
    "DEBUG": "false",
    "SECRET_KEY": "test-only-django-secret-key",
    "BG_AUTH_JWT_SIGNING_KEY": "test-only-jwt-signing-key",
    "KORFBAL_AUDIT_INGEST_TOKEN": "test-only-audit-token",
}


def _import_production_settings(**overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        [sys.executable, "-c", "import korfbal.settings"],
        cwd=PROJECT_DIR,
        env=os.environ | PRODUCTION_ENV | overrides,
        check=False,
        capture_output=True,
        text=True,
    )


def test_cookie_settings_are_exported_by_settings_entrypoint() -> None:
    """Custom cookie settings must survive the explicit security import list."""
    expected_settings = (
        "CSRF_COOKIE_DOMAIN",
        "CSRF_COOKIE_NAME",
        "CSRF_COOKIE_SECURE",
        "CSRF_COOKIE_VERSION",
        "SESSION_COOKIE_DOMAIN",
        "SESSION_COOKIE_NAME",
        "SESSION_COOKIE_SECURE",
    )

    for setting_name in expected_settings:
        assert getattr(settings, setting_name) == getattr(security, setting_name)


@pytest.mark.parametrize(
    "missing_setting",
    ["BG_AUTH_JWT_SIGNING_KEY", "KORFBAL_AUDIT_INGEST_TOKEN"],
)
def test_production_startup_requires_security_tokens(missing_setting: str) -> None:
    """Production startup must reject missing signing and ingest credentials."""
    result = _import_production_settings(**{missing_setting: ""})

    assert result.returncode != 0
    assert f"Environment variable '{missing_setting}' is required" in result.stderr
