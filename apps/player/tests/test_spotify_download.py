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
from yt_dlp.utils import match_filter_func

from apps.player.adapters.outbound import song_downloader as downloader
from apps.player.application.ports import CommandRunOptions, TrackMetadata


SPOTIFY_URL = "https://open.spotify.com/track/27CXrzqx1N44o1Pi6AHRT4"
EXPECTED_CALLS = 2
VIDEO_DURATION = 60
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700


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


@pytest.mark.parametrize(
    ("diagnostic", "message", "reason"),
    [
        (
            "ERROR: Sign in to confirm you\u2019re not a bot. Use --cookies",
            "YouTube is blocking this import",
            "youtube_sign_in_required",
        ),
        (
            "ERROR: Sign in to confirm you're not a bot. Use --cookies",
            "YouTube is blocking this import",
            "youtube_sign_in_required",
        ),
        (
            "ERROR: HTTP Error 429: Too Many Requests",
            "YouTube is temporarily limiting imports",
            "youtube_rate_limited",
        ),
        (
            "WARNING: The provided YouTube account cookies are no longer valid",
            "YouTube import session has expired or is invalid",
            "youtube_session_invalid",
        ),
    ],
)
def test_provider_rejection_is_specific_redacted_and_not_repeated_immediately(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    diagnostic: str,
    message: str,
    reason: str,
) -> None:
    """Known provider denials must not be reported as invalid songs or timeouts."""
    secret = uuid4().hex
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess(
        [],
        1,
        stderr=f"{diagnostic}\nCookie: {secret}\nhttps://private.example/{secret}",
    )
    (tmp_path / "audio.mp3").write_bytes(b"partial")
    with pytest.raises(RuntimeError, match=message) as error:
        downloader.download_song(
            "https://www.youtube.com/watch?v=SSbBvKaM6sk&t=8s",
            tmp_path,
            command_runner=runner,
            metadata_client=Mock(),
        )
    runner.run.assert_called_once()
    command = runner.run.call_args.args[0]
    assert command[-1] == "https://www.youtube.com/watch?v=SSbBvKaM6sk"
    assert "--cookies" not in command
    assert reason in caplog.text
    assert secret not in str(error.value)
    assert secret not in caplog.text
    assert not (tmp_path / "audio.mp3").exists()


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_optional_session_uses_private_disposable_cookie_copy(
    tmp_path: Path, outcome: str
) -> None:
    """An importer cannot modify its read-only secret or leave credentials behind."""
    secret = uuid4().hex
    source = tmp_path / "mounted-session.txt"
    source.write_text(f"# Netscape HTTP Cookie File\n# {secret}\n", encoding="utf-8")
    source.chmod(0o400)
    before = source.read_bytes()
    cookie_paths: list[Path] = []
    output = tmp_path / "audio"

    def run(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        cookie_file = Path(cmd[cmd.index("--cookies") + 1])
        cookie_paths.append(cookie_file)
        assert cookie_file != source
        filters = [
            cmd[index + 1]
            for index, value in enumerate(cmd)
            if value == "--match-filter"
        ]
        matcher = match_filter_func(filters)
        for availability in ("public", "unlisted"):
            assert (
                matcher(
                    {
                        "availability": availability,
                        "duration": VIDEO_DURATION,
                        "is_live": False,
                    },
                    incomplete=False,
                )
                is None
            )
        for availability in (
            "private",
            "needs_auth",
            "premium_only",
            "subscriber_only",
            None,
        ):
            assert (
                matcher(
                    {
                        "availability": availability,
                        "duration": VIDEO_DURATION,
                        "is_live": False,
                    },
                    incomplete=False,
                )
                is not None
            )
        assert (
            matcher(
                {"availability": "public", "duration": 901, "is_live": False},
                incomplete=False,
            )
            is not None
        )
        assert (
            matcher(
                {
                    "availability": "unlisted",
                    "duration": VIDEO_DURATION,
                    "is_live": True,
                },
                incomplete=False,
            )
            is not None
        )
        assert cookie_file.stat().st_mode & 0o777 == PRIVATE_FILE_MODE
        assert cookie_file.parent.stat().st_mode & 0o777 == PRIVATE_DIRECTORY_MODE
        assert secret not in " ".join(cmd)
        assert source.read_bytes() == before
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n# provider refresh\n", encoding="utf-8"
        )
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(list(cmd), options.timeout)
        if outcome == "failure":
            return subprocess.CompletedProcess(list(cmd), 1, stderr="Failed")
        (output / "audio.mp3").write_bytes(b"complete synthetic audio")
        return subprocess.CompletedProcess(list(cmd), 0)

    with override_settings(YOUTUBE_COOKIES_FILE=str(source)):
        if outcome == "success":
            downloader.download_song(
                SPOTIFY_URL,
                output,
                command_runner=FakeCommandRunner(run),
                metadata_client=FakeMetadata(),
            )
        else:
            with pytest.raises(RuntimeError):
                downloader.download_song(
                    SPOTIFY_URL,
                    output,
                    command_runner=FakeCommandRunner(run),
                    metadata_client=FakeMetadata(),
                )
    assert source.read_bytes() == before
    assert cookie_paths
    assert all(not path.exists() for path in cookie_paths)


def test_missing_configured_session_fails_without_an_anonymous_attempt(
    tmp_path: Path,
) -> None:
    """A missing mounted secret is an operational error, never a public path leak."""
    secret_path = tmp_path / uuid4().hex / "cookies.txt"
    runner = Mock()
    with (
        override_settings(YOUTUBE_COOKIES_FILE=str(secret_path)),
        pytest.raises(RuntimeError, match="authentication is unavailable") as error,
    ):
        downloader.download_song(
            "https://youtu.be/SSbBvKaM6sk?t=8s",
            tmp_path,
            command_runner=runner,
            metadata_client=Mock(),
        )
    runner.run.assert_not_called()
    assert str(secret_path) not in str(error.value)
    assert error.value.__cause__ is None
