"""Pytest configuration for the korfbal Django project.

CI runs the korfbal settings with SSL redirect enabled. Most API tests call
endpoints via plain HTTP (the default test client scheme), which would cause
301 redirects and make status code assertions brittle.

For tests we disable `SECURE_SSL_REDIRECT` by default; individual tests that
need to verify redirect behaviour can override this explicitly.
"""

from __future__ import annotations

import pytest
from pytest_django.fixtures import SettingsWrapper


@pytest.fixture(autouse=True)
def _disable_secure_ssl_redirect(settings: SettingsWrapper) -> None:
    settings.SECURE_SSL_REDIRECT = False
