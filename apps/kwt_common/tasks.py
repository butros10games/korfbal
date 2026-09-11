"""One Celery execution envelope for durable application work."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from celery import current_app, shared_task
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.kwt_common.models import BackgroundJob


MAX_ATTEMPTS = 5
# Longer than the hard task limit, including worker shutdown margin.
RECOVERY_SECONDS = 2100
QUEUE_LIMITS = {"celery": 120, "projections": 300, "media": 2000}


@contextmanager
def _exclusive(job_id: int) -> Iterator[bool]:
    """Prevent overlapping attempts, including an expired lease's old process.

    Session locks release automatically on worker death. SQLite is used only by
    sequential unit tests; production and concurrency tests use PostgreSQL.

    Yields:
        Whether this worker owns the execution lock.

    """
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [7419, job_id])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [7419, job_id])


@shared_task(
    ignore_result=True,
    soft_time_limit=1950,
    time_limit=2000,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_job(job_id: int) -> None:
    """Execute under ownership, then wake successors after releasing the lock."""
    try:
        _execute_job(job_id)
    finally:
        transaction.on_commit(lambda: publish_job(job_id), robust=True)


def _execute_job(job_id: int) -> None:
    """Run latest committed input; persist retries and completion, not results."""
    with _exclusive(job_id) as acquired:
        if not acquired:
            return
        with transaction.atomic():
            job = BackgroundJob.objects.select_for_update().filter(pk=job_id).first()
            if job is None or job.due_at is None or job.due_at > timezone.now():
                return
            if job.attempts >= MAX_ATTEMPTS:
                job.due_at = None
                job.error = "WorkerLost"
                job.save()
                return
            job.attempts += 1
            job.due_at = timezone.now() + timedelta(
                seconds=QUEUE_LIMITS.get(job.queue, 2000) + 100
            )
            job.save()
        try:
            current_app.tasks[job.task].run(*job.args, **job.kwargs)
        except Exception as exc:
            # Store the exception type only: provider messages can contain secrets.
            with transaction.atomic():
                latest = BackgroundJob.objects.select_for_update().get(pk=job_id)
                newer = latest.generation != job.generation
                latest.error = type(exc).__name__
                latest.attempts = 0 if newer else job.attempts
                latest.due_at = (
                    timezone.now()
                    + timedelta(seconds=0 if newer else min(30 * 2**job.attempts, 900))
                    if newer or job.attempts < MAX_ATTEMPTS
                    else None
                )
                latest.published_until = None
                latest.save()
            raise
        else:
            with transaction.atomic():
                latest = BackgroundJob.objects.select_for_update().get(pk=job_id)
                latest.completed_generation = job.generation
                latest.due_at = (
                    timezone.now() if latest.generation != job.generation else None
                )
                if latest.due_at is None:
                    latest.args, latest.kwargs = [], {}
                latest.attempts = 0
                latest.error = ""
                latest.published_until = None
                latest.save()


def publish_job(job_id: int) -> bool:
    """Publish committed work with a short ETA; reserve against duplicate wakeups."""
    now = timezone.now()
    with transaction.atomic():
        job = (
            BackgroundJob.objects
            .select_for_update()
            .filter(pk=job_id, due_at__lte=now + timedelta(seconds=60))
            .filter(Q(published_until=None) | Q(published_until__lte=now))
            .first()
        )
        if job is None:
            return False
        reservation = max(now, job.due_at) + timedelta(minutes=5)
        job.published_until = reservation
        job.save(update_fields=["published_until"])
    limit = QUEUE_LIMITS.get(job.queue, 2000)
    options = {"eta": job.due_at} if job.due_at > now else {}
    try:
        execute_job.apply_async(
            args=[job.pk],
            queue=job.queue,
            expires=reservation,
            soft_time_limit=limit - 10,
            time_limit=limit,
            **options,
        )
    except Exception:
        BackgroundJob.objects.filter(pk=job.pk, published_until=reservation).update(
            published_until=None
        )
        raise
    return True


@shared_task(ignore_result=True, soft_time_limit=20, time_limit=25)
def dispatch_due_jobs() -> int:
    """Recover missed dispatches and promote near-term deadlines once a minute."""
    count = 0
    now = timezone.now()
    for queue in QUEUE_LIMITS:
        ids = list(
            BackgroundJob.objects
            .filter(
                Q(published_until=None) | Q(published_until__lte=now),
                queue=queue,
                due_at__lte=now + timedelta(seconds=60),
            )
            .order_by("due_at")
            .values_list("pk", flat=True)[:25]
        )
        count += sum(publish_job(job_id) for job_id in ids)
    return count
