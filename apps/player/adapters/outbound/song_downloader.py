"""Download one Spotify search result or an exact YouTube video as bounded audio."""

import json
import math
from pathlib import Path
import subprocess

from django.conf import settings

from apps.player.application.ports import (
    CommandRunner,
    CommandRunOptions,
    DownloadedSong,
    SongDownloadError,
    TrackMetadataClient,
)
from apps.player.services.upload_validation import MAX_AUDIO_UPLOAD_BYTES
from apps.player.song_sources import MAX_SONG_SECONDS, parse_song_source


def download_song(
    source_url: str,
    output_dir: Path,
    *,
    command_runner: CommandRunner,
    metadata_client: TrackMetadataClient,
) -> DownloadedSong:
    """Download one video; consult Spotify only for Spotify sources.

    Raises:
        SongDownloadError: No complete, bounded MP3 was produced.

    """
    source = parse_song_source(source_url)
    metadata = None
    query = source.url
    if source.provider == "spotify":
        metadata = metadata_client.get_track(source.url.rsplit("/", 1)[-1])
        query = (
            f"ytsearch1:{' '.join(metadata.artists)} {metadata.title} official audio"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "audio.mp3"
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
        "--match-filter",
        f"duration <= {MAX_SONG_SECONDS} & !is_live",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "128K",
        "--print",
        "after_move:%(.{title,artist,uploader,duration})j",
        "--output",
        str(output_dir / "audio.%(ext)s"),
        "--",
        query,
    ]
    timed_out = False
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
            timed_out = True
            continue
        except FileNotFoundError as error:
            raise SongDownloadError(
                "Audio importer is unavailable. Please try later."
            ) from error
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
    output.unlink(missing_ok=True)
    if timed_out:
        raise SongDownloadError("Download timed out. Please retry.")
    raise SongDownloadError(
        "Could not import audio. Use a publicly available video of at most 15 minutes "
        "and 25 MB. Live, private or restricted videos cannot be imported. "
        "Please retry or upload an MP3."
    )


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
