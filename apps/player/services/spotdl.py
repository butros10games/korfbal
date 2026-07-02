"""spotDL download service."""

from __future__ import annotations

from collections.abc import Sequence
import logging
from pathlib import Path
import subprocess  # nosec B404
from uuid import uuid4

from django.conf import settings

from apps.player.services.command_runner import (
    DEFAULT_COMMAND_RUNNER,
    CommandRunner,
    CommandRunOptions,
)
from apps.player.spotify import canonicalize_spotify_track_url


logger = logging.getLogger(__name__)


def validate_spotify_url_for_spotdl(spotify_url: str) -> None:
    """Validate Spotify URL before passing it to a subprocess.

    Raises:
        ValueError: If the URL is missing, malformed, or unsafe for argv use.

    """
    min_printable_ascii = 32

    value = (spotify_url or "").strip()
    if not value:
        raise ValueError("Spotify URL is required")

    if any(ch.isspace() for ch in value) or any(
        ord(ch) < min_printable_ascii for ch in value
    ):
        raise ValueError("Spotify URL contains invalid whitespace/control characters")

    _ = canonicalize_spotify_track_url(value)


def pick_downloaded_audio(output_dir: Path) -> Path:
    """Pick the most likely downloaded audio file.

    Raises:
        FileNotFoundError: If spotDL produced no supported audio file.

    """
    candidates: list[Path] = []
    for pattern in ("*.mp3", "*.m4a", "*.opus", "*.ogg", "*.wav", "*.flac"):
        candidates.extend(output_dir.rglob(pattern))

    if not candidates:
        raise FileNotFoundError("spotDL finished but no audio file was produced")

    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


def redact_spotdl_command(cmd: list[str]) -> str:
    """Return a log-safe command string."""
    redacted: list[str] = []
    redact_next = False
    redact_flags = {
        "--client-id",
        "--client-secret",
        "--auth-token",
    }
    for part in cmd:
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue

        redacted.append(part)
        if part in redact_flags:
            redact_next = True

    return " ".join(redacted)


def spotdl_base_args() -> list[str]:
    """Build base spotDL argv list without the action/query arguments."""
    args = ["spotdl"]

    client_id = str(getattr(settings, "SPOTIFY_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(settings, "SPOTIFY_CLIENT_SECRET", "") or "").strip()

    if client_id and client_secret:
        args.extend(["--client-id", client_id, "--client-secret", client_secret])
    elif client_id or client_secret:
        logger.warning(
            "SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET are partially configured; "
            "spotDL may fail. Configure both to use custom Spotify credentials."
        )

    return args


def _spotdl_commands(*, spotify_url: str, output_dir: Path) -> list[list[str]]:
    output_template = str(output_dir / "{title}.{output-ext}")
    base = spotdl_base_args()
    return [
        [
            *base,
            "download",
            spotify_url,
            "--output",
            output_template,
            "--format",
            "mp3",
            "--threads",
            "1",
        ],
        [
            *base,
            spotify_url,
            "--output",
            output_template,
            "--format",
            "mp3",
            "--threads",
            "1",
        ],
    ]


def download_spotify_track(
    spotify_url: str,
    output_dir: Path,
    *,
    command_runner: CommandRunner | None = None,
) -> Path:
    """Download a Spotify link using spotDL into the given directory.

    Raises:
        RuntimeError: If spotDL cannot download a usable audio file.

    """
    validate_spotify_url_for_spotdl(spotify_url)
    output_dir.mkdir(parents=True, exist_ok=True)

    timeout_seconds = int(getattr(settings, "SPOTDL_DOWNLOAD_TIMEOUT_SECONDS", 60 * 15))
    runner = command_runner or DEFAULT_COMMAND_RUNNER

    attempted: list[tuple[list[str], str]] = []
    last_error = ""

    for cmd in _spotdl_commands(spotify_url=spotify_url, output_dir=output_dir):
        logger.info("Running spotDL: %s", redact_spotdl_command(cmd))
        try:
            proc = runner.run(
                cmd,
                CommandRunOptions(
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    shell=False,
                ),
            )
        except subprocess.TimeoutExpired as exc:
            try:
                downloaded = pick_downloaded_audio(output_dir)
            except FileNotFoundError:
                attempted.append((cmd, "TIMEOUT"))
                last_error = (
                    f"Download timed out after {timeout_seconds} seconds. Please retry."
                )
                logger.warning(
                    "spotDL timed out after %s seconds (no output file found)",
                    timeout_seconds,
                    exc_info=exc,
                )
                continue
            else:
                logger.warning(
                    "spotDL timed out after %s seconds but produced %s; accepting file",
                    timeout_seconds,
                    downloaded,
                )
                return downloaded

        attempted.append((cmd, (proc.stdout or "")[-2000:]))
        if proc.returncode == 0:
            return pick_downloaded_audio(output_dir)
        last_error = (proc.stderr or proc.stdout or "").strip()[-4000:]

    redacted_details = "\n".join(redact_spotdl_command(cmd) for cmd, _ in attempted)
    logger.warning(
        "spotDL failed. Tried:\n%s\nLast error:\n%s",
        redacted_details,
        last_error,
    )
    if "timed out" in last_error.lower():
        raise RuntimeError(last_error)
    raise RuntimeError("Download failed. Please retry.")


class DummySpotdlRunner:
    """Test/dev helper that creates a small dummy MP3 file."""

    def run(
        self,
        cmd: Sequence[str],
        options: CommandRunOptions,
    ) -> subprocess.CompletedProcess[str]:
        """Create a dummy output file and report success."""
        del options
        cmd_list = list(cmd)
        out_idx = cmd_list.index("--output") + 1
        output_dir = Path(cmd_list[out_idx]).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{uuid4().hex}.mp3"
        output_path.write_bytes(b"ID3")
        return subprocess.CompletedProcess(cmd_list, 0, stdout="ok", stderr="")
