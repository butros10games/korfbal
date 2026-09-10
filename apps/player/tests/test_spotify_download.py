"""Tests for bounded catalogue-driven audio downloads.

Provider metadata and subprocess results are synthetic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import subprocess
from uuid import uuid4

from django.test import override_settings
import pytest

from apps.player.application.ports import CommandRunOptions, TrackMetadata
from apps.player.services import spotify_download as downloader


SPOTIFY_URL = "https://open.spotify.com/track/27CXrzqx1N44o1Pi6AHRT4"
EXPECTED_CALLS = 2


class FakeMetadata:
    """Supply a catalogue identity without contacting Spotify."""

    def get_track(self, track_id: str) -> TrackMetadata:
        """Accept only the canonical ID extracted from the input URL."""
        assert track_id == SPOTIFY_URL.rsplit("/", 1)[-1]
        return TrackMetadata(title="Synthetic track", artists=("Synthetic artist",))


class FakeCommandRunner:
    """Command adapter backed by a test callback."""

    def __init__(
        self,
        run: Callable[
            [Sequence[str], CommandRunOptions],
            subprocess.CompletedProcess[str],
        ],
    ) -> None:
        """Create a command runner backed by the callback."""
        self._run = run

    def run(
        self,
        cmd: Sequence[str],
        options: CommandRunOptions,
    ) -> subprocess.CompletedProcess[str]:
        """Delegate to the test callback."""
        return self._run(cmd, options)


@override_settings(SPOTDL_DOWNLOAD_TIMEOUT_SECONDS=1)
def test_download_timeout_then_success(
    tmp_path: Path,
) -> None:
    """A timeout on the first invocation should not immediately fail the download."""
    calls = 0

    def fake_run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1

        assert options.check is False
        assert options.capture_output is True
        assert options.text is True
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
        (output_dir / "audio.mp3").write_bytes(b"ID3" + (b"0" * 2048))
        return subprocess.CompletedProcess(cmd_list, 0, stdout="ok", stderr="")

    downloaded = downloader.download_spotify_track(
        SPOTIFY_URL,
        tmp_path,
        command_runner=FakeCommandRunner(fake_run),
        metadata_client=FakeMetadata(),
    )
    assert downloaded.exists()
    assert downloaded.suffix == ".mp3"
    assert calls == EXPECTED_CALLS


@override_settings(SPOTDL_DOWNLOAD_TIMEOUT_SECONDS=1)
def test_download_all_timeouts_raises_user_friendly_error(
    tmp_path: Path,
) -> None:
    """If all attempts time out and no file is produced, raise a clear error."""

    def fake_run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=list(cmd), timeout=options.timeout)

    with pytest.raises(RuntimeError) as excinfo:
        _ = downloader.download_spotify_track(
            SPOTIFY_URL,
            tmp_path,
            command_runner=FakeCommandRunner(fake_run),
            metadata_client=FakeMetadata(),
        )

    assert "timed out" in str(excinfo.value).lower()


def test_download_keeps_credentials_out_of_process_arguments(tmp_path: Path) -> None:
    """Only a search string and fixed controls enter the downloader process."""
    client_secret = uuid4().hex

    def fake_run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "yt-dlp"
        assert "--ignore-config" in cmd
        assert "--no-plugin-dirs" in cmd
        assert "--no-playlist" in cmd
        assert "--max-filesize" in cmd
        assert cmd[-2] == "--"
        assert cmd[-1] == "ytsearch1:Synthetic artist Synthetic track official audio"
        assert client_secret not in " ".join(cmd)
        (tmp_path / "audio.mp3").write_bytes(b"synthetic audio")
        return subprocess.CompletedProcess(list(cmd), 0)

    with override_settings(SPOTIFY_CLIENT_SECRET=client_secret):
        assert downloader.download_spotify_track(
            SPOTIFY_URL,
            tmp_path,
            command_runner=FakeCommandRunner(fake_run),
            metadata_client=FakeMetadata(),
        ).is_file()


def test_timeout_never_accepts_partial_mp3(tmp_path: Path) -> None:
    """A terminated conversion cannot publish a partial result as ready."""

    def fake_run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        (tmp_path / "audio.mp3").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(list(cmd), options.timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        downloader.download_spotify_track(
            SPOTIFY_URL,
            tmp_path,
            command_runner=FakeCommandRunner(fake_run),
            metadata_client=FakeMetadata(),
        )
    assert not (tmp_path / "audio.mp3").exists()


def test_success_without_bounded_output_is_rejected(tmp_path: Path) -> None:
    """A successful subprocess cannot publish an oversized MP3."""

    def fake_run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        with (tmp_path / "audio.mp3").open("wb") as stream:
            stream.truncate(26 * 1024 * 1024)
        return subprocess.CompletedProcess(list(cmd), 0)

    with pytest.raises(RuntimeError, match="Download failed"):
        downloader.download_spotify_track(
            SPOTIFY_URL,
            tmp_path,
            command_runner=FakeCommandRunner(fake_run),
            metadata_client=FakeMetadata(),
        )
    assert not (tmp_path / "audio.mp3").exists()
