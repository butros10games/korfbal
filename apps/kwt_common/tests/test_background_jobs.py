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


@pytest.mark.parametrize("fails", [False, True], ids=["success", "failure"])
def test_successor_keeps_its_requested_deadline(handler: Mock, fails: bool) -> None:
    """A cleanup or capacity retry must not immediately requeue itself in a loop."""
    job = enqueue(TASK, "deferred", queue="vision")
    deadline = timezone.now() + timedelta(hours=1)

    def defer() -> None:
        recovery_deadline = BackgroundJob.objects.get(pk=job.pk).due_at
        enqueue(TASK, "deferred", queue="vision", due_at=deadline)
        assert BackgroundJob.objects.get(pk=job.pk).due_at == recovery_deadline
        if fails:
            raise RuntimeError("Try later")

    handler.side_effect = defer
    if fails:
        with pytest.raises(RuntimeError):
            execute_job.run(job.pk)
    else:
        execute_job.run(job.pk)
    job.refresh_from_db()
    assert job.due_at == deadline
    execute_job.run(job.pk)
    handler.assert_called_once()


@pytest.mark.parametrize("delays", [(60, 120), (120, 60), (120, 0)])
def test_successor_coalesces_to_earliest_requested_deadline(
    handler: Mock, delays: tuple[int, int]
) -> None:
    """Later scheduling cannot postpone an earlier request, including an edit now."""
    job = enqueue(TASK, "coalesced")
    now = timezone.now()

    def defer() -> None:
        for delay in delays:
            enqueue(TASK, "coalesced", due_at=now + timedelta(seconds=delay))

    handler.side_effect = defer
    execute_job.run(job.pk)
    job.refresh_from_db()
    assert job.due_at == now + timedelta(seconds=min(delays))


def test_worker_death_preserves_future_successor_and_resets_its_retry_budget(
    handler: Mock,
) -> None:
    """Recovering a dead attempt must neither run the successor early nor exhaust it."""
    job = enqueue(TASK, "dead-worker")
    BackgroundJob.objects.filter(pk=job.pk).update(attempts=MAX_ATTEMPTS - 1)
    deadline = timezone.now() + timedelta(hours=2)

    def interrupted() -> None:
        enqueue(TASK, "dead-worker", due_at=deadline)
        raise SystemExit

    handler.side_effect = interrupted
    with pytest.raises(SystemExit):
        execute_job.run(job.pk)
    job.refresh_from_db()
    assert job.due_at is not None
    with patch("apps.kwt_common.tasks.timezone.now", return_value=job.due_at):
        execute_job.run(job.pk)
    handler.assert_called_once()
    job.refresh_from_db()
    assert job.due_at == deadline
    assert job.attempts == 0
    handler.side_effect = None
    handler.reset_mock()
    with patch("apps.kwt_common.tasks.timezone.now", return_value=deadline):
        execute_job.run(job.pk)
    job.refresh_from_db()
    handler.assert_called_once()
    assert job.due_at is None
    assert job.completed_generation == job.generation


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


@pytest.mark.parametrize("queue", ["media", "vision"])
def test_broker_failure_does_not_lose_intent(handler: Mock, queue: str) -> None:
    """A failed publish remains due and succeeds on the next dispatcher pass."""
    job = enqueue(TASK, "broker", queue=queue)
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
        assert publish.call_args.kwargs["queue"] == queue
        assert "eta" not in publish.call_args.kwargs
    execute_job.run(job.pk)
    handler.assert_called_once()


def test_vision_worker_and_recovery_outlast_clip_deadline(handler: Mock) -> None:
    """Long clips must publish their receipt before the worker or recovery fires."""
    job = enqueue(TASK, "long-clip", queue="vision")
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        assert dispatch_due_jobs.run() == 1
        limits = publish.call_args.kwargs
    assert limits["soft_time_limit"] > 3600 + 60
    assert limits["time_limit"] > limits["soft_time_limit"]

    def observe_lease() -> None:
        job.refresh_from_db()
        assert job.due_at > timezone.now() + timedelta(seconds=limits["time_limit"])

    handler.side_effect = observe_lease
    execute_job.run(job.pk)
    handler.assert_called_once()
    job.refresh_from_db()
    assert job.due_at is None


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


def test_earlier_request_wakes_work_already_published_with_a_later_eta(
    handler: Mock,
) -> None:
    """Urgent edits supersede a broker timer without letting its old delivery replay."""
    with patch("apps.kwt_common.tasks.execute_job.apply_async") as publish:
        with TestCase.captureOnCommitCallbacks(execute=True):
            job = enqueue(TASK, "urgent", due_at=timezone.now() + timedelta(seconds=50))
        assert "eta" in publish.call_args.kwargs
        publish.reset_mock()
        with TestCase.captureOnCommitCallbacks(execute=True):
            enqueue(TASK, "urgent", args=["latest"])
        publish.assert_called_once()
        assert "eta" not in publish.call_args.kwargs
    execute_job.run(job.pk)
    execute_job.run(job.pk)
    handler.assert_called_once_with("latest")
