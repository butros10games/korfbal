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
app.conf.beat_schedule = {}
