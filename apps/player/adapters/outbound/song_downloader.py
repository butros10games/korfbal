"""Download one Spotify search result or an exact YouTube video as bounded audio."""

from collections.abc import Iterator
from contextlib import contextmanager
import json
import logging
import math
from pathlib import Path
import subprocess
import tempfile

from django.conf import settings

from apps.player.application.ports import (
    CommandRunner,
    CommandRunOptions,
    DownloadedSong,
    SongDownloadError,
    TrackMetadata,
    TrackMetadataClient,
)
from apps.player.services.upload_validation import MAX_AUDIO_UPLOAD_BYTES
from apps.player.song_sources import MAX_SONG_SECONDS, parse_song_source


logger = logging.getLogger(__name__)


def download_song(
    source_url: str,
    output_dir: Path,
    *,
    command_runner: CommandRunner,
    metadata_client: TrackMetadataClient,
) -> DownloadedSong:
    """Download one video; consult Spotify only for Spotify sources."""
    source = parse_song_source(source_url)
    metadata = None
    query = source.url
    if source.provider == "spotify":
        metadata = metadata_client.get_track(source.url.rsplit("/", 1)[-1])
        query = (
            f"ytsearch1:{' '.join(metadata.artists)} {metadata.title} official audio"
        )
    with _youtube_cookie_file() as cookie_file:
        return _download_audio(
            query=query,
            metadata=metadata,
            output_dir=output_dir,
            cookie_file=cookie_file,
            command_runner=command_runner,
        )


@contextmanager
def _youtube_cookie_file() -> Iterator[Path | None]:
    """Give each job a private writable copy of an optional read-only secret.

    yt-dlp updates its cookie jar when exiting, even when extraction fails.
    Never let concurrent imports overwrite the mounted credential file.

    Yields:
        The temporary cookie path, or None for anonymous downloads.

    Raises:
        SongDownloadError: The configured session file cannot be read.

    """
    configured = getattr(settings, "YOUTUBE_COOKIES_FILE", "").strip()
    if not configured:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="korfbal_youtube_session_") as directory:
        cookie_file = Path(directory) / "cookies.txt"
        try:
            with cookie_file.open("xb") as handle:
                cookie_file.chmod(0o600)
                handle.write(Path(configured).read_bytes())
        except OSError:
            raise SongDownloadError(
                "YouTube import authentication is unavailable. "
                "Ask the administrator to check the import session, or upload an MP3."
            ) from None
        yield cookie_file


def _download_audio(
    *,
    query: str,
    metadata: TrackMetadata | None,
    output_dir: Path,
    cookie_file: Path | None,
    command_runner: CommandRunner,
) -> DownloadedSong:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "audio.mp3"
    match_filter = f"duration <= {MAX_SONG_SECONDS} & !is_live"
    filters = (
        [f"{match_filter} & availability = {value}" for value in ("public", "unlisted")]
        if cookie_file is not None
        else [match_filter]
    )
    command = [
        "yt-dlp",
        "--ignore-config",
        "--no-plugin-dirs",
        "--js-runtimes",
        "node",
        "--no-playlist",
        "--no-progress",
        "--format",
        "bestaudio/best",
        "--socket-timeout",
        "15",
        "--retries",
        "1",
        "--max-filesize",
        str(MAX_AUDIO_UPLOAD_BYTES),
        *[argument for value in filters for argument in ("--match-filter", value)],
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "128K",
        "--print",
        "after_move:%(.{title,artist,uploader,duration})j",
        "--output",
        str(output_dir / "audio.%(ext)s"),
        *(["--cookies", str(cookie_file)] if cookie_file is not None else []),
        "--",
        query,
    ]
    last_timed_out = False
    for _ in range(2):
        output.unlink(missing_ok=True)
        try:
            result = command_runner.run(
                command,
                CommandRunOptions(
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=int(settings.SPOTDL_DOWNLOAD_TIMEOUT_SECONDS),
                    kill_process_tree=True,
                ),
            )
        except subprocess.TimeoutExpired:
            last_timed_out = True
            continue
        except FileNotFoundError as error:
            raise SongDownloadError(
                "Audio importer is unavailable. Please try later."
            ) from error
        last_timed_out = False
        if (
            result.returncode == 0
            and output.is_file()
            and 0 < output.stat().st_size <= MAX_AUDIO_UPLOAD_BYTES
        ):
            info = _read_metadata(result.stdout)
            duration = info.get("duration")
            return DownloadedSong(
                path=output,
                title=metadata.title[:255] if metadata else _text(info.get("title")),
                artists=(
                    ", ".join(metadata.artists)[:255]
                    if metadata
                    else _text(info.get("artist") or info.get("uploader"))
                ),
                duration_seconds=(
                    int(duration)
                    if isinstance(duration, (int, float))
                    and math.isfinite(duration)
                    and 0 <= duration <= MAX_SONG_SECONDS
                    else None
                ),
            )
        failure = _provider_failure(result.stderr)
        if failure:
            reason, message = failure
            logger.warning("Goal-song provider rejected import: %s", reason)
            output.unlink(missing_ok=True)
            raise SongDownloadError(message)
    output.unlink(missing_ok=True)
    if last_timed_out:
        raise SongDownloadError("Download timed out. Please retry.")
    raise SongDownloadError(
        "Could not import audio. Use a publicly available video of at most 15 minutes "
        "and 25 MB. Live, private or restricted videos cannot be imported. "
        "Please retry or upload an MP3."
    )


def _provider_failure(stderr: str | None) -> tuple[str, str] | None:
    """Classify provider errors without exposing diagnostics, URLs or cookies."""
    diagnostic = (stderr or "").lower()
    if "sign in to confirm" in diagnostic and "not a bot" in diagnostic:
        return (
            "youtube_sign_in_required",
            "YouTube is blocking this import and requires a verified session. "
            "Ask the administrator to reconnect YouTube imports, or upload an MP3.",
        )
    if "http error 429" in diagnostic or "too many requests" in diagnostic:
        return (
            "youtube_rate_limited",
            "YouTube is temporarily limiting imports. Please try again later "
            "or upload an MP3.",
        )
    if "cookies" in diagnostic and (
        "no longer valid" in diagnostic or "does not look like" in diagnostic
    ):
        return (
            "youtube_session_invalid",
            "The YouTube import session has expired or is invalid. "
            "Ask the administrator to reconnect YouTube imports, or upload an MP3.",
        )
    return None


def _text(value: object) -> str:
    return value[:255] if isinstance(value, str) else ""


def _read_metadata(stdout: str | None) -> dict:
    for line in reversed((stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}
