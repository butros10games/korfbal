"""Web process isolation keeps failures and shutdown visible to the container."""

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from time import monotonic, sleep

from korfbal.metrics_exporter import LiveWorkerCollector
from korfbal.serve import metrics_commands, server_commands, server_environment
from prometheus_client import CollectorRegistry
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
OPEN_STREAMS = 7
WORKER_REQUESTS = 2


def test_web_pools_reuse_database_connections_by_default() -> None:
    """Hosts without an explicit size must not open a connection per request."""
    assert server_environment({})["KORFBAL_DB_POOL_MAX_SIZE"] == "4"
    assert server_environment({"KORFBAL_DB_POOL_MAX_SIZE": "0"}) == {
        "KORFBAL_DB_POOL_MAX_SIZE": "0"
    }


def test_pool_commands_isolate_stream_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long-lived stream limits must not change API or public-read admission."""
    monkeypatch.setenv("KORFBAL_SSE_BACKPRESSURE", "12000")
    commands = server_commands()
    assert {command[command.index("--port") + 1] for command in commands} == {
        "1664",
        "1665",
        "1666",
        "1667",
    }
    assert "--backpressure" not in commands[0]
    assert commands[1][commands[1].index("--backpressure") + 1] != "12000"
    assert commands[2][commands[2].index("--backpressure") + 1] == "12000"
    assert commands[1][-1] == "korfbal.wsgi:application"


def test_asgi_pools_skip_lifespan() -> None:
    """Django rejects lifespan scopes, which Sentry would report on every start."""
    interfaces = {
        command[command.index("--port") + 1]: command[command.index("--interface") + 1]
        for command in server_commands()
    }
    assert interfaces == {
        "1664": "asginl",
        "1665": "wsgi",
        "1666": "asginl",
        "1667": "wsgi",
    }


def test_supervisor_stops_all_pools_on_container_signal(tmp_path: Path) -> None:
    """Exercise real child processes instead of only checking command construction."""
    probe = (
        "import signal,time,sys; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        "Path(sys.argv[1]).touch(); time.sleep(30)"
    )
    commands = [
        [sys.executable, "-c", probe, str(tmp_path / str(index))] for index in range(3)
    ]
    launcher = (
        "import json,sys; from korfbal.serve import supervise; "
        "sys.exit(supervise(json.loads(sys.argv[1])))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, json.dumps(commands)],
        cwd=Path(__file__).resolve().parents[3],
    )
    try:
        deadline = monotonic() + 10
        while len(list(tmp_path.iterdir())) != len(commands) and monotonic() < deadline:
            sleep(0.05)
        assert len(list(tmp_path.iterdir())) == len(commands)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_supervisor_exits_when_a_pool_fails() -> None:
    """A dead pool must not leave a superficially healthy deployment unit."""
    commands = [[sys.executable, "-c", "raise SystemExit(7)"]]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from korfbal.serve import supervise; "
            "sys.exit(supervise(json.loads(sys.argv[1])))",
            json.dumps(commands),
        ],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        timeout=10,
    )
    assert result.returncode == 1


def test_metrics_stay_off_unless_prometheus_is_enabled(tmp_path: Path) -> None:
    """Without the flag, no exporter starts and processes keep their own registry."""
    environment = server_environment({})
    assert "PROMETHEUS_MULTIPROC_DIR" not in environment
    assert metrics_commands(environment) == []

    enabled = server_environment({
        "KORFBAL_ENABLE_PROMETHEUS": "true",
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path / "metrics"),
    })
    stale = tmp_path / "metrics" / "counter_123.db"
    stale.parent.mkdir()
    stale.write_bytes(b"old")
    assert metrics_commands(enabled) == [
        [sys.executable, "-m", "korfbal.metrics_exporter"]
    ]
    assert not stale.exists()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_exporter_sums_samples_from_every_web_process(tmp_path: Path) -> None:
    """A scrape reports all workers, not whichever process answered it."""
    port = _free_port()
    environment = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        "KORFBAL_METRICS_PORT": str(port),
        "KORFBAL_BIND_HOST": "127.0.0.1",
    }
    record = (
        "from prometheus_client import Counter; Counter('korfbal_probe', 'probe').inc()"
    )
    for _ in range(3):
        subprocess.run(
            [sys.executable, "-c", record], env=environment, check=True, timeout=30
        )
    exporter = subprocess.Popen(
        [sys.executable, "-m", "korfbal.metrics_exporter"],
        cwd=PROJECT_ROOT,
        env=environment,
    )
    try:
        deadline = monotonic() + 15
        body = ""
        while monotonic() < deadline:
            connection = HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", "/metrics")
                body = connection.getresponse().read().decode()
                break
            except OSError:
                sleep(0.1)
            finally:
                connection.close()
        assert "korfbal_probe_total 3.0" in body
        exporter.send_signal(signal.SIGTERM)
        assert exporter.wait(timeout=10) == 0
    finally:
        if exporter.poll() is None:
            exporter.kill()
            exporter.wait(timeout=10)


def test_exporter_forgets_live_gauges_of_respawned_workers(tmp_path: Path) -> None:
    """A worker killed and respawned in place must not keep reporting its streams."""
    environment = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    record = (
        "from prometheus_client import Counter, Gauge;"
        "Counter('korfbal_probe', 'probe').inc();"
        "Gauge('korfbal_streams', 'open', multiprocess_mode='livesum')"
        f".set({OPEN_STREAMS})"
    )
    dead = subprocess.run(
        [sys.executable, "-c", f"{record}; import os; print(os.getpid())"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    dead_pid = int(dead.stdout.strip())
    live = subprocess.Popen(
        [sys.executable, "-c", f"{record}; import sys; sys.stdin.read()"],
        env=environment,
        stdin=subprocess.PIPE,
    )
    try:
        deadline = monotonic() + 15
        while not list(tmp_path.glob(f"gauge_livesum_{live.pid}.db")):
            assert monotonic() < deadline
            sleep(0.05)
        samples = {
            sample.name: sample.value
            for metric in LiveWorkerCollector(
                CollectorRegistry(), str(tmp_path)
            ).collect()
            for sample in metric.samples
        }
    finally:
        live.communicate(timeout=10)
    assert samples["korfbal_streams"] == pytest.approx(OPEN_STREAMS)
    assert samples["korfbal_probe_total"] == pytest.approx(WORKER_REQUESTS)
    assert not list(tmp_path.glob(f"gauge_livesum_{dead_pid}.db"))


@pytest.mark.parametrize(("multiprocess", "route"), [(True, False), (False, True)])
def test_application_ports_serve_metrics_only_without_the_exporter(
    tmp_path: Path, multiprocess: bool, route: bool
) -> None:
    """With the internal exporter, published API ports expose no `/metrics` route."""
    environment = {
        **os.environ,
        "KORFBAL_ENABLE_PROMETHEUS": "true",
        "DJANGO_SETTINGS_MODULE": "korfbal.settings",
    }
    environment.pop("PROMETHEUS_MULTIPROC_DIR", None)
    if multiprocess:
        environment["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; django.setup();"
            "from django.urls import Resolver404, resolve\n"
            "try:\n    resolve('/metrics')\n    print(True)\n"
            "except Resolver404:\n    print(False)",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == str(route)
