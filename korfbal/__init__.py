"""Django config package for korfbal project.

Import the Celery app on Django startup so that `@shared_task` uses the
project's configured Celery instance (and therefore the configured broker).

This mirrors the standard Celery+Django integration pattern used in the other
projects in this monorepo.
"""

from .celery import app as celery_app


__all__ = ("celery_app",)
