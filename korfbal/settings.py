"""Deprecated settings module.

The settings entrypoint has moved to the `korfbal.settings` package.

Keep this module temporarily to avoid breaking imports in existing deployments,
but prefer `DJANGO_SETTINGS_MODULE=korfbal.settings`.
"""

from __future__ import annotations

from korfbal.settings import *  # noqa: F403
