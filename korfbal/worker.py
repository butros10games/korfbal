"""Supervise isolated Celery pools in the existing worker deployment unit."""

import os
import signal
import subprocess
import sys


POOLS = {
    "celery": ("celery,instant", 2),
    "projections": ("projections", 2),
    "media": ("media", 1),
    "competition": ("competition", 1),
}


def worker_commands() -> list[list[str]]:
    """Keep authentication responsive while media and provider jobs are busy."""
    return [
        [
            "celery",
            "-A",
            "korfbal",
            "worker",
            "--loglevel=info",
            "--queues",
            queues,
            "--hostname",
            f"{name}@%h",
            "--concurrency",
            str(
                max(
                    1,
                    int(os.getenv(f"KORFBAL_{name.upper()}_CONCURRENCY", str(default))),
                )
            ),
        ]
        for name, (queues, default) in POOLS.items()
    ]


def main() -> None:
    """Forward beat/management commands; fail the container if any pool dies.

    Raises:
        SystemExit: Exit with failure when any worker unexpectedly exits.

    """
    args = sys.argv[1:]
    if args and args != ["worker"] and not (args[0] == "celery" and "worker" in args):
        # Docker command forwarding must replace PID 1 to preserve signal delivery.
        os.execvp(args[0], args)  # noqa: S606
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
        for command in worker_commands():
            if stopping:
                break
            processes.append(subprocess.Popen(command))
        if processes and not stopping:
            os.wait()
    finally:
        was_stopping = stopping
        stop(signal.SIGTERM, None)
        for process in processes:
            process.wait()
    raise SystemExit(0 if was_stopping else 1)


if __name__ == "__main__":
    main()
