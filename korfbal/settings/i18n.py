"""Internationalization / localization."""

from __future__ import annotations

from .env import env


LANGUAGE_CODE = env("LANGUAGE_CODE", "en-us")
TIME_ZONE = env("TIME_ZONE", "Europe/Amsterdam")
USE_I18N = True
USE_TZ = True
