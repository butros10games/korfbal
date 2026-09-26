"""Transactional scheduling and bounded recovery shared by Korfbal jobs."""

from datetime import datetime
from typing import Any, TypedDict, Unpack

from django.db import transaction
from django.utils import timezone

from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.tasks import publish_job


class JobOptions(TypedDict, total=False):
    """Optional dispatch policy; defaults suit repeatable short jobs."""

    queue: str
    due_at: datetime | None
    once: bool


@transaction.atomic
def enqueue(
    task: str,
    key: str,
    *,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    **options: Unpack[JobOptions],
) -> BackgroundJob:
    """Persist intent with its owner transaction; repeated mutations coalesce.

    Running work retains its recovery deadline. Requests arriving during execution
    retain their earliest deadline and run after the current attempt finishes. One-shot
    keys are retained even after completion to make delayed duplicates harmless.
    """
    due_at = options.get("due_at") or timezone.now()
    queue = options.get("queue", "celery")
    once = options.get("once", False)
    job, created = BackgroundJob.objects.select_for_update().get_or_create(
        key=f"{task}:{key}",
        defaults={
            "task": task,
            "args": args or [],
            "kwargs": kwargs or {},
            "queue": queue,
            "due_at": due_at,
        },
    )
    if not created and not once:
        job.generation += 1
        job.args, job.kwargs = args or [], kwargs or {}
        if job.due_at is None:
            job.due_at = due_at
            job.next_due_at = None
            job.published_until = None
            job.attempts = 0
        elif job.attempts == 0:
            if due_at < job.due_at:
                job.due_at = due_at
                job.published_until = None
        else:
            job.next_due_at = min(job.next_due_at or due_at, due_at)
        job.save()
    transaction.on_commit(lambda: publish_job(job.pk), robust=True)
    return job
