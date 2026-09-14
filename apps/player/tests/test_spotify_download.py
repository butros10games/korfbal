"""Tests for bounded catalogue-driven audio downloads.

Provider metadata and subprocess results are synthetic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import subprocess
from unittest.mock import Mock
from uuid import uuid4

from django.test import override_settings
import pytest

from apps.player.adapters.outbound import song_downloader as downloader
from apps.player.application.ports import CommandRunOptions, TrackMetadata


SPOTIFY_URL = "https://open.spotify.com/track/27CXrzqx1N44o1Pi6AHRT4"
EXPECTED_CALLS = 2
VIDEO_DURATION = 60


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

    downloaded = downloader.download_song(
        SPOTIFY_URL,
        tmp_path,
        command_runner=FakeCommandRunner(fake_run),
        metadata_client=FakeMetadata(),
    )
    assert downloaded.path.exists()
    assert downloaded.path.suffix == ".mp3"
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
        _ = downloader.download_song(
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
        assert downloader.download_song(
            SPOTIFY_URL,
            tmp_path,
            command_runner=FakeCommandRunner(fake_run),
            metadata_client=FakeMetadata(),
        ).path.is_file()


def test_timeout_never_accepts_partial_mp3(tmp_path: Path) -> None:
    """A terminated conversion cannot publish a partial result as ready."""

    def fake_run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        (tmp_path / "audio.mp3").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(list(cmd), options.timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        downloader.download_song(
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

    with pytest.raises(RuntimeError, match="Could not import audio"):
        downloader.download_song(
            SPOTIFY_URL,
            tmp_path,
            command_runner=FakeCommandRunner(fake_run),
            metadata_client=FakeMetadata(),
        )
    assert not (tmp_path / "audio.mp3").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/BaW_jenozKc?si=share&t=42",
        "https://music.youtube.com/watch?v=BaW_jenozKc",
        "https://www.youtube.com/shorts/BaW_jenozKc",
    ],
)
def test_youtube_download_uses_exact_video_and_imports_metadata(
    tmp_path: Path, url: str
) -> None:
    """Direct video imports never consult Spotify or search for a replacement."""
    metadata = Mock()

    def run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        assert cmd[-1] == "https://www.youtube.com/watch?v=BaW_jenozKc"
        assert "--no-playlist" in cmd
        assert cmd[cmd.index("--format") + 1] == "bestaudio/best"
        assert options.kill_process_tree
        (tmp_path / "audio.mp3").write_bytes(b"complete synthetic audio")
        return subprocess.CompletedProcess(
            list(cmd),
            0,
            stdout='{"title":"Test sound","uploader":"Test channel","duration":60.8}',
        )

    downloaded = downloader.download_song(
        url,
        tmp_path,
        command_runner=FakeCommandRunner(run),
        metadata_client=metadata,
    )
    metadata.get_track.assert_not_called()
    assert downloaded.path.read_bytes() == b"complete synthetic audio"
    assert downloaded.title == "Test sound"
    assert downloaded.artists == "Test channel"
    assert downloaded.duration_seconds == VIDEO_DURATION


def test_restricted_video_failure_does_not_expose_process_output(
    tmp_path: Path,
) -> None:
    """A provider failure exposes a usable explanation, never its raw diagnostics."""
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess(
        [], 1, stderr="private provider diagnostics"
    )
    with pytest.raises(RuntimeError, match="publicly available") as error:
        downloader.download_song(
            "https://youtu.be/BaW_jenozKc",
            tmp_path,
            command_runner=runner,
            metadata_client=Mock(),
        )
    assert "private provider diagnostics" not in str(error.value)
    assert not (tmp_path / "audio.mp3").exists()
