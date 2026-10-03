"""Serve the Prometheus metrics of every web process from one endpoint.

Granian runs several worker processes per pool, and each one records its own
samples. With ``PROMETHEUS_MULTIPROC_DIR`` set, they write to shared files that
this process aggregates, so a scrape no longer sees whichever worker happened to
answer. It runs beside the pools, outside Django, so the scrape needs neither an
allowed host, HTTPS nor a public route.
"""

from collections.abc import Iterable
import os
from pathlib import Path
import re
import signal
import sys

from prometheus_client import CollectorRegistry, Metric, start_http_server
from prometheus_client.multiprocess import MultiProcessCollector, mark_process_dead


DEFAULT_PORT = "9464"
_LIVE_GAUGE_FILE = re.compile(r"^gauge_live\w+_(\d+)\.db$")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def prune_dead_live_gauges(directory: Path) -> None:
    """Drop live gauges (open connections, streams) of workers that have exited.

    Granian respawns failed workers in place, so a whole-directory cleanup at
    container start does not cover them. Counters and histograms stay: their
    totals must survive a worker's exit.
    """
    pids = {
        int(match.group(1))
        for entry in directory.iterdir()
        if (match := _LIVE_GAUGE_FILE.match(entry.name))
    }
    for pid in pids:
        if not _alive(pid):
            mark_process_dead(pid, str(directory))


class LiveWorkerCollector(MultiProcessCollector):
    """Aggregate every worker's files, ignoring live gauges of exited workers."""

    def __init__(self, registry: CollectorRegistry, path: str) -> None:
        """Register with the registry, reading samples from ``path``."""
        super().__init__(registry, path)
        self._directory = Path(path)

    def collect(self) -> Iterable[Metric]:
        """Prune exited workers' live gauges, then merge the remaining files.

        Returns:
            The merged metrics.

        """
        prune_dead_live_gauges(self._directory)
        return super().collect()


def main() -> None:
    """Serve until the supervisor stops the container."""
    registry = CollectorRegistry()
    LiveWorkerCollector(registry, os.environ["PROMETHEUS_MULTIPROC_DIR"])
    start_http_server(
        int(os.getenv("KORFBAL_METRICS_PORT", DEFAULT_PORT)),
        addr=os.getenv("KORFBAL_BIND_HOST", "127.0.0.1"),
        registry=registry,
    )
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
