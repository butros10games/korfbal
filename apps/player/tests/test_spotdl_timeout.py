"""Tests for spotDL wrapper behavior.

We don't run spotDL in tests; we simulate subprocess behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4

from django.test import override_settings
import pytest

from apps.player.services import spotdl
from apps.player.services.command_runner import CommandRunOptions


SPOTIFY_URL = "https://open.spotify.com/track/27CXrzqx1N44o1Pi6AHRT4"
EXPECTED_CALLS = 2


@override_settings(SPOTDL_DOWNLOAD_TIMEOUT_SECONDS=1)
def test_run_spotdl_timeout_then_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A timeout on the first invocation should not immediately fail the download."""
    calls = 0

    def fake_run(cmd: Sequence[str], options: CommandRunOptions) -> SimpleNamespace:
        nonlocal calls
        calls += 1

        assert options.check is False
        assert options.capture_output is True
        assert options.text is True
        assert options.shell is False
        assert options.timeout == 1

        cmd_list = list(cmd)

        if calls == 1:
            raise subprocess.TimeoutExpired(
                cmd=cmd_list,
                timeout=options.timeout,
            )

        # Simulate a successful run producing an mp3 somewhere under output_dir.
        out_idx = cmd_list.index("--output") + 1
        output_template = cmd_list[out_idx]
        output_dir = Path(output_template).parent
        (output_dir / "result.mp3").write_bytes(b"ID3" + (b"0" * 2048))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(spotdl.DEFAULT_COMMAND_RUNNER, "run", fake_run)

    downloaded = spotdl.download_spotify_track(SPOTIFY_URL, tmp_path)
    assert downloaded.exists()
    assert downloaded.suffix == ".mp3"
    assert calls == EXPECTED_CALLS


@override_settings(SPOTDL_DOWNLOAD_TIMEOUT_SECONDS=1)
def test_run_spotdl_all_timeouts_raises_user_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If all attempts time out and no file is produced, raise a clear error."""

    def fake_run(cmd: Sequence[str], options: CommandRunOptions) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=list(cmd), timeout=options.timeout)

    monkeypatch.setattr(spotdl.DEFAULT_COMMAND_RUNNER, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        _ = spotdl.download_spotify_track(SPOTIFY_URL, tmp_path)

    assert "timed out" in str(excinfo.value).lower()


@override_settings(SPOTDL_DOWNLOAD_TIMEOUT_SECONDS=1)
def test_run_spotdl_passes_spotify_client_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured Spotify client credentials should be passed to spotDL."""
    client_id = "client-id"
    # Avoid hardcoding secrets in tests (ruff S106).
    client_secret = uuid4().hex

    def fake_run(cmd: Sequence[str], options: CommandRunOptions) -> SimpleNamespace:
        del options
        cmd_list = list(cmd)
        assert "--client-id" in cmd_list
        assert "--client-secret" in cmd_list
        assert cmd_list[cmd_list.index("--client-id") + 1] == client_id
        assert cmd_list[cmd_list.index("--client-secret") + 1] == client_secret

        # Simulate success producing an mp3 somewhere under output_dir.
        out_idx = cmd_list.index("--output") + 1
        output_template = cmd_list[out_idx]
        output_dir = Path(output_template).parent
        (output_dir / "result.mp3").write_bytes(b"ID3" + (b"0" * 2048))

        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(spotdl.DEFAULT_COMMAND_RUNNER, "run", fake_run)

    with override_settings(
        SPOTIFY_CLIENT_ID=client_id,
        SPOTIFY_CLIENT_SECRET=client_secret,
    ):
        downloaded = spotdl.download_spotify_track(SPOTIFY_URL, tmp_path)

    assert downloaded.exists()
