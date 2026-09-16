"""Web process isolation keeps failures and shutdown visible to the container."""

import json
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic, sleep

from korfbal.serve import server_commands
import pytest


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
