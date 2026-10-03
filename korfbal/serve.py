"""Supervise separate API, live-read, public-read and SSE capacity in the web image."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


def server_commands() -> list[list[str]]:
    """Keep spectator work away from authenticated mutations and admin requests."""
    common = [
        sys.executable,
        "-m",
        "granian",
        "--no-ws",
        "--host",
        os.getenv("KORFBAL_BIND_HOST", "127.0.0.1"),
    ]
    return [
        [
            *common,
            "--interface",
            "asgi",
            "--port",
            "1664",
            "--workers",
            os.getenv("GRANIAN_WORKERS", "4"),
            "korfbal.asgi:application",
        ],
        [
            *common,
            "--interface",
            "wsgi",
            "--port",
            "1665",
            "--workers",
            os.getenv("KORFBAL_PUBLIC_WORKERS", "2"),
            "--blocking-threads",
            os.getenv("KORFBAL_PUBLIC_THREADS", "2"),
            "--backpressure",
            os.getenv("KORFBAL_PUBLIC_BACKPRESSURE", "16384"),
            "korfbal.wsgi:application",
        ],
        [
            *common,
            "--interface",
            "asgi",
            "--port",
            "1666",
            "--workers",
            os.getenv("KORFBAL_SSE_WORKERS", "2"),
            "--backpressure",
            os.getenv("KORFBAL_SSE_BACKPRESSURE", "8192"),
            "korfbal.asgi:application",
        ],
        [
            *common,
            "--interface",
            "wsgi",
            "--port",
            "1667",
            "--workers",
            os.getenv("KORFBAL_LIVE_READ_WORKERS", "2"),
            "--blocking-threads",
            os.getenv("KORFBAL_LIVE_READ_THREADS", "2"),
            "--backpressure",
            os.getenv("KORFBAL_LIVE_READ_BACKPRESSURE", "16384"),
            "korfbal.wsgi:application",
        ],
    ]


# Without a pool every request opens a new PostgreSQL connection. Ten web processes
# at this size stay well inside the default 100 connections beside Celery, which
# keeps direct connections for its session advisory locks.
WEB_DB_POOL_MAX_SIZE = "4"


# Every web process writes its samples here; `korfbal.metrics_exporter` sums them.
DEFAULT_METRICS_DIRECTORY = str(Path(tempfile.gettempdir()) / "korfbal-prometheus")


def metrics_enabled(environ: dict[str, str]) -> bool:
    """Match the settings flag that installs django-prometheus."""
    value = environ.get("KORFBAL_ENABLE_PROMETHEUS", "")
    return value.lower() in {"1", "true", "yes", "on"}


def server_environment(environ: dict[str, str]) -> dict[str, str]:
    """Pool web database connections unless the deployment sets its own size."""
    environment = {"KORFBAL_DB_POOL_MAX_SIZE": WEB_DB_POOL_MAX_SIZE, **environ}
    if metrics_enabled(environment):
        environment.setdefault("PROMETHEUS_MULTIPROC_DIR", DEFAULT_METRICS_DIRECTORY)
    return environment


def metrics_commands(environ: dict[str, str]) -> list[list[str]]:
    """Start the aggregate exporter with a directory free of a previous run's files.

    Stale files from crashed processes would otherwise keep reporting old counters
    and connection gauges after a container restart.
    """
    directory = environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not directory:
        return []
    shutil.rmtree(directory, ignore_errors=True)
    Path(directory).mkdir(parents=True)
    return [[sys.executable, "-m", "korfbal.metrics_exporter"]]


def supervise(commands: list[list[str]], env: dict[str, str] | None = None) -> int:
    """Forward stop signals and fail the deployment unit when any pool exits."""
    processes: list[subprocess.Popen] = []
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            if stopping:
                break
            processes.append(subprocess.Popen(command, env=env))
        if processes and not stopping:
            os.wait()
    finally:
        requested = stopping
        stop(signal.SIGTERM, None)
        for process in processes:
            process.wait()
    return 0 if requested else 1


if __name__ == "__main__":
    environment = server_environment(dict(os.environ))
    raise SystemExit(
        supervise(
            [*server_commands(), *metrics_commands(environment)],
            environment,
        )
    )
