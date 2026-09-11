"""Durability contracts shared by media, notifications and match projections."""

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.services.jobs import enqueue
from apps.kwt_common.tasks import MAX_ATTEMPTS, dispatch_due_jobs, execute_job


pytestmark = pytest.mark.django_db
TASK = "test.work"


@pytest.fixture
def handler() -> Iterator[Mock]:
    """Replace only the external handler.

    Yields:
        The mocked external task handler.

    """
    run = Mock()
    with patch(
        "apps.kwt_common.tasks.current_app",
        SimpleNamespace(tasks={TASK: SimpleNamespace(run=run)}),
    ):
        yield run


def test_intent_rolls_back_with_domain_transaction() -> None:
    """No committed domain change means no background work."""
    with transaction.atomic():
        enqueue(TASK, "rollback")
        transaction.set_rollback(True)
    assert not BackgroundJob.objects.exists()


def test_many_requests_execute_latest_input_once(handler: Mock) -> None:
    """Bursts combine without dropping the latest payload."""
    requests = 20
    for value in range(requests):
        job = enqueue(TASK, "match", kwargs={"revision": value})
    assert BackgroundJob.objects.count() == 1
    execute_job.run(job.pk)
    execute_job.run(job.pk)
    handler.assert_called_once_with(revision=19)
    job.refresh_from_db()
    assert job.completed_generation == job.generation == requests
    assert job.due_at is None


def test_mutation_during_execution_is_processed_again(handler: Mock) -> None:
    """A request arriving after claim must survive the older completion."""
    job = enqueue(TASK, "match", args=[1])
    handler.side_effect = lambda *args: enqueue(TASK, "match", args=[2])
    execute_job.run(job.pk)
    job.refresh_from_db()
    assert job.completed_generation == 1
    assert job.generation == job.completed_generation + 1
    assert job.due_at is not None
    handler.side_effect = None
    execute_job.run(job.pk)
    assert handler.call_args.args == (2,)


def test_one_shot_delivery_survives_duplicate_requests(handler: Mock) -> None:
    """Completed notification keys remain deduplicated."""
    job = enqueue(TASK, "recipient", once=True)
    execute_job.run(job.pk)
    duplicate = enqueue(TASK, "recipient", once=True)
    execute_job.run(duplicate.pk)
    handler.assert_called_once()


def test_future_work_stays_in_database_until_due(handler: Mock) -> None:
    """Multi-hour MVP deadlines do not create Celery ETA reservations."""
    job = enqueue(TASK, "mvp", due_at=timezone.now() + timedelta(hours=3))
    execute_job.run(job.pk)
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        assert dispatch_due_jobs.run() == 0
    publish.assert_not_called()
    handler.assert_not_called()


def test_broker_failure_does_not_lose_intent(handler: Mock) -> None:
    """A failed publish remains due and succeeds on the next dispatcher pass."""
    job = enqueue(TASK, "broker", queue="media")
    with (
        patch(
            "apps.kwt_common.tasks.execute_job.apply_async", side_effect=ConnectionError
        ),
        pytest.raises(ConnectionError),
    ):
        dispatch_due_jobs.run()
    job.refresh_from_db()
    assert job.due_at <= timezone.now()
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        assert dispatch_due_jobs.run() == 1
        assert publish.call_args.kwargs["queue"] == "media"
        assert "eta" not in publish.call_args.kwargs
    execute_job.run(job.pk)
    handler.assert_called_once()


def test_failures_back_off_and_stop_without_storing_sensitive_messages(
    handler: Mock,
) -> None:
    """Retries are bounded and error diagnostics cannot expose provider secrets."""
    job = enqueue(TASK, "failure")
    handler.side_effect = RuntimeError("private provider token")
    for attempt in range(MAX_ATTEMPTS):
        BackgroundJob.objects.filter(pk=job.pk).update(due_at=timezone.now())
        with pytest.raises(RuntimeError):
            execute_job.run(job.pk)
        job.refresh_from_db()
        assert job.attempts == attempt + 1
        assert job.error == "RuntimeError"
    assert job.due_at is None
    job = enqueue(TASK, "failure")
    assert job.attempts == 0
    handler.side_effect = None
    execute_job.run(job.pk)
    job.refresh_from_db()
    assert not job.error
    assert job.completed_generation == job.generation


def test_abandoned_attempt_is_recovered_after_deadline(handler: Mock) -> None:
    """Process death leaves an expiring deadline that permits recovery."""
    job = enqueue(TASK, "interrupted")
    BackgroundJob.objects.filter(pk=job.pk).update(
        attempts=1, due_at=timezone.now() - timedelta(seconds=1)
    )
    execute_job.run(job.pk)
    handler.assert_called_once()
    job.refresh_from_db()
    assert job.due_at is None


def test_duplicate_worker_does_not_execute_while_lock_is_owned(handler: Mock) -> None:
    """Expired delivery must not overlap a still-running attempt."""
    job = enqueue(TASK, "locked")
    with patch("apps.kwt_common.tasks._exclusive") as lock:
        lock.return_value.__enter__.return_value = False
        execute_job.run(job.pk)
    handler.assert_not_called()


def test_queued_messages_are_not_republished_on_every_tick() -> None:
    """A saturated pool does not flood its broker; lost messages can still recover."""
    job = enqueue(TASK, "queued", queue="media")
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        assert dispatch_due_jobs.run() == 1
        assert dispatch_due_jobs.run() == 0
        publish.assert_called_once()
        BackgroundJob.objects.filter(pk=job.pk).update(published_until=timezone.now())
        assert dispatch_due_jobs.run() == 1


def test_operator_can_find_and_retry_failures_but_not_completed_jobs(
    handler: Mock,
) -> None:
    """Operational retries are explicit and cannot reopen a delivered notification."""
    job = enqueue(TASK, "operator", once=True)
    BackgroundJob.objects.filter(pk=job.pk).update(
        due_at=None, error="RuntimeError", attempts=MAX_ATTEMPTS
    )
    output = StringIO()
    call_command("background_jobs", stdout=output)
    assert json.loads(output.getvalue())["failed"][0]["pk"] == job.pk
    call_command("background_jobs", retry_id=job.pk, stdout=StringIO())
    execute_job.run(job.pk)
    with pytest.raises(CommandError, match="No exhausted unfinished job"):
        call_command("background_jobs", retry_id=job.pk, stdout=StringIO())
    handler.assert_called_once()


def test_commit_dispatches_once_for_a_burst_and_rollback_dispatches_nothing() -> None:
    """Normal work reaches Celery at commit without waiting for a scanner."""
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        with TestCase.captureOnCommitCallbacks(execute=True), transaction.atomic():
            for _ in range(10):
                job = enqueue(TASK, "commit")
            publish.assert_not_called()
        publish.assert_called_once()
        assert publish.call_args.kwargs["args"] == [job.pk]
        publish.reset_mock()
        with TestCase.captureOnCommitCallbacks(execute=True), transaction.atomic():
            enqueue(TASK, "rolled-back")
            transaction.set_rollback(True)
        publish.assert_not_called()


def test_committed_intent_survives_immediate_broker_failure() -> None:
    """Post-commit publication failure does not report the domain write as failed."""
    with (
        patch(
            "apps.kwt_common.tasks.execute_job.apply_async", side_effect=ConnectionError
        ),
        TestCase.captureOnCommitCallbacks(execute=True),
    ):
        job = enqueue(TASK, "outage")
    job.refresh_from_db()
    assert job.published_until is None
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        assert dispatch_due_jobs.run() == 1
        publish.assert_called_once()


def test_short_deadline_dispatches_with_eta_and_long_deadline_waits() -> None:
    """Debounce uses short ETAs; multi-hour deadlines remain in PostgreSQL."""
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        with TestCase.captureOnCommitCallbacks(execute=True):
            soon = enqueue(TASK, "soon", due_at=timezone.now() + timedelta(seconds=2))
            later = enqueue(TASK, "later", due_at=timezone.now() + timedelta(hours=2))
        publish.assert_called_once()
        assert publish.call_args.kwargs["eta"] == soon.due_at
        publish.reset_mock()
        deadline = timezone.now() + timedelta(seconds=30)
        BackgroundJob.objects.filter(pk=later.pk).update(due_at=deadline)
        assert dispatch_due_jobs.run() == 1
        assert publish.call_args.kwargs["eta"] == deadline
