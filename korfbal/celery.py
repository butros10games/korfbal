"""Celery configuration."""

import os

from celery import Celery


# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "korfbal.settings")

app = Celery("korfbal")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# Namespace 'CELERY' means all celery-related configs must be prefixed with 'CELERY_'.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Automatically discover tasks in installed apps.
app.autodiscover_tasks()

# Celery Beat schedule for periodic tasks.
app.conf.beat_schedule = {
    "dispatch-durable-jobs": {
        "task": "apps.kwt_common.tasks.dispatch_due_jobs",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    "discover-private-match-forms": {
        "task": "apps.competition.tasks.discover_match_forms",
        "schedule": 60.0,
        "options": {"expires": 60, "queue": "competition"},
    },
    "sync-current-competition": {
        "task": "apps.competition.tasks.sync_current_competition",
        "schedule": 60.0,
        "options": {"expires": 60, "queue": "competition"},
    },
}
