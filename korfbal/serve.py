"""Supervise separate API, live-read, public-read and SSE capacity in the web image."""

import os
import signal
import subprocess
import sys


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


def supervise(commands: list[list[str]]) -> int:
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
            processes.append(subprocess.Popen(command))
        if processes and not stopping:
            os.wait()
    finally:
        requested = stopping
        stop(signal.SIGTERM, None)
        for process in processes:
            process.wait()
    return 0 if requested else 1


if __name__ == "__main__":
    raise SystemExit(supervise(server_commands()))
