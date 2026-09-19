"""Worker capacity and queue isolation for interactive jobs."""

from korfbal.worker import POOLS, worker_commands
import pytest


def test_instant_has_dedicated_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """General jobs cannot occupy the slot reserved for interactive actions."""
    for name in POOLS:
        monkeypatch.delenv(f"KORFBAL_{name.upper()}_CONCURRENCY", raising=False)
    commands = worker_commands()
    consumers = {
        command[command.index("--queues") + 1]: command for command in commands
    }
    assert set(consumers) == {
        "celery",
        "instant",
        "projections",
        "media",
        "competition",
    }
    for queue, concurrency in {"celery": "2", "instant": "1"}.items():
        command = consumers[queue]
        assert command[command.index("--concurrency") + 1] == concurrency
        assert command[command.index("--hostname") + 1] == f"{queue}@%h"
    assert len({
        command[command.index("--hostname") + 1] for command in commands
    }) == len(consumers)


@pytest.mark.parametrize(("configured", "expected"), [("3", "3"), ("0", "1")])
def test_instant_capacity_is_independent(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    expected: str,
) -> None:
    """Operators can tune instant capacity without starving either consumer."""
    monkeypatch.setenv("KORFBAL_INSTANT_CONCURRENCY", configured)
    monkeypatch.setenv("KORFBAL_CELERY_CONCURRENCY", "4")
    consumers = {
        command[command.index("--queues") + 1]: command for command in worker_commands()
    }
    instant, general = consumers["instant"], consumers["celery"]
    assert instant[instant.index("--concurrency") + 1] == expected
    assert general[general.index("--concurrency") + 1] == "4"
