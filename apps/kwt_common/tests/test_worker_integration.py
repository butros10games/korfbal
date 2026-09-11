"""Real PostgreSQL/Valkey/prefork recovery; enabled with KORFBAL_TEST_BROKER_URL."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

from django.db import connection
from django.test import override_settings
from django.utils import timezone
from korfbal.celery import app
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.services.jobs import enqueue


@contextmanager
def _worker(queue: str, broker: str, log: Path) -> Iterator[subprocess.Popen]:
    database = connection.settings_dict
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "korfbal.settings_test",
        "DJANGO_TEST_USE_POSTGRES": "1",
        "KORFBAL_TEST_DB_LANE": "",
        "POSTGRES_DB": database["NAME"],
        "POSTGRES_HOST": database["HOST"],
        "POSTGRES_PORT": str(database["PORT"]),
        "POSTGRES_USER": database["USER"],
        "POSTGRES_PASSWORD": database["PASSWORD"],
    }
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "korfbal",
        "--broker",
        broker,
        "worker",
        "--pool=prefork",
        "--concurrency=1",
        "--queues",
        queue,
        "--hostname",
        f"{queue}@%h",
        "--loglevel=warning",
        "--without-gossip",
        "--include",
        "apps.kwt_common.tests.worker_probe",
    ]
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[3],
            start_new_session=True,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = monotonic() + 30
    while monotonic() < deadline:
        if condition():
            return
        sleep(0.1)
    pytest.fail("Worker did not reach the expected durable state within 30 seconds")


@pytest.mark.django_db(transaction=True)
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_prefork_worker_recovers_after_process_death(tmp_path: Path) -> None:
    """A killed worker's task is recovered and duplicate messages do not repeat work."""
    broker = os.getenv("KORFBAL_TEST_BROKER_URL")
    if connection.vendor != "postgresql" or not broker:
        pytest.skip("Requires isolated PostgreSQL and KORFBAL_TEST_BROKER_URL")
    queue = f"wt-{uuid4().hex}"
    marker = BackgroundJob.objects.create(
        key=queue, task="marker", due_at=None, error="hold"
    )
    with app.connection_for_write(url=broker) as broker_connection:
        with _worker(queue, broker, tmp_path / "first-worker.log") as worker:
            with override_settings(CELERY_BROKER_URL=broker):
                job = enqueue(
                    "apps.kwt_common.tests.worker_probe.probe",
                    queue,
                    args=[marker.pk],
                    queue=queue,
                )
            _wait_until(
                lambda: BackgroundJob.objects.filter(
                    pk=marker.pk, error="entered"
                ).exists()
            )
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=10)
        job.refresh_from_db()
        assert job.completed_generation == 0
        assert job.due_at > timezone.now()
        # Advance the recovery deadline rather than waiting the production timeout.
        BackgroundJob.objects.filter(pk=job.pk).update(due_at=timezone.now())
        BackgroundJob.objects.filter(pk=marker.pk).update(error="")
        with _worker(queue, broker, tmp_path / "second-worker.log"):
            for _ in range(2):
                app.send_task(
                    "apps.kwt_common.tasks.execute_job",
                    args=[job.pk],
                    queue=queue,
                    connection=broker_connection,
                )
            _wait_until(
                lambda: BackgroundJob.objects.filter(
                    pk=job.pk, completed_generation=1
                ).exists()
            )
            sleep(1)
        marker.refresh_from_db()
        assert marker.completed_generation == 1
