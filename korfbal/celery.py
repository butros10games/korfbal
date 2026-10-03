"""Celery configuration."""

import os

from celery import Celery, signals
from celery.schedules import crontab

from korfbal.observability import TaskToken, bind_task, unbind_task


# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "korfbal.settings")

app = Celery("korfbal")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# Namespace 'CELERY' means all celery-related configs must be prefixed with 'CELERY_'.
app.config_from_object("django.conf:settings", namespace="CELERY")
# Keep Django's LOGGING (JSON lines with task IDs) instead of Celery's own format.
app.conf.worker_hijack_root_logger = False

# Automatically discover tasks in installed apps.
app.autodiscover_tasks()

# Celery Beat schedule for periodic tasks.
app.conf.beat_schedule = {
    "sync-video-artifacts": {
        "task": "apps.video_analysis.tasks.sync_files",
        "schedule": 60.0,
        "options": {"expires": 60, "queue": "vision"},
    },
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
    "run-provider-turn": {
        "task": "apps.competition.tasks.run_provider_turn",
        "schedule": 60.0,
        "options": {"expires": 60, "queue": "competition"},
    },
    "publish-competition-backlog": {
        "task": "apps.competition.tasks.publish_competition_backlog",
        "schedule": 60.0,
        "options": {"expires": 60, "queue": "publication"},
    },
    "sync-competition-history": {
        "task": "apps.competition.tasks.sync_competition_history",
        "schedule": 60.0,
        "options": {"expires": 300, "queue": "competition"},
    },
    "refresh-club-team-ratings": {
        "task": "apps.competition.tasks.refresh_club_team_ratings",
        "schedule": 900.0,
        "options": {"expires": 900, "queue": "celery"},
    },
    "replay-club-team-ratings": {
        "task": "apps.competition.tasks.refresh_club_team_ratings",
        "schedule": crontab(minute="30", hour="3"),
        "kwargs": {"full": True},
        "options": {"expires": 3600, "queue": "celery"},
    },
    "recheck-competition-history": {
        "task": "apps.competition.tasks.recheck_competition_history",
        "schedule": crontab(minute="0", hour="4", day_of_week="monday"),
        "options": {"expires": 3600, "queue": "competition"},
    },
}


_task_log_context: dict[str, TaskToken] = {}


@signals.task_prerun.connect
def _bind_task_log_context(task_id: str, task: object, **_: object) -> None:
    _task_log_context[task_id] = bind_task(task_id, getattr(task, "name", "unknown"))


@signals.task_postrun.connect
def _unbind_task_log_context(task_id: str, **_: object) -> None:
    unbind_task(_task_log_context.pop(task_id, None))
