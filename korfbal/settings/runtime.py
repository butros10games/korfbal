"""Runtime environment flags (DEBUG/test runner/etc.)."""

from __future__ import annotations

import os
import sys

from .env import env, env_bool


DJANGO_ENV = env("DJANGO_ENV", "development").lower()
DEBUG = env_bool("DEBUG", DJANGO_ENV != "production")
SECRET_KEY = env("SECRET_KEY", "change-me" if DEBUG else None, required=not DEBUG)

RUNNER = env("RUNNER", "")
KORFBAL_ENABLE_PROMETHEUS = env_bool("KORFBAL_ENABLE_PROMETHEUS", RUNNER == "uwsgi")

RUNNING_TESTS = bool(os.getenv("PYTEST_CURRENT_TEST")) or any(
    "pytest" in arg for arg in sys.argv
)
