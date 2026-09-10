"""Download a single catalogue track without a bundled web-server dependency."""

from pathlib import Path
import subprocess

from django.conf import settings

from apps.player.application.ports import (
    CommandRunner,
    CommandRunOptions,
    TrackMetadataClient,
)
from apps.player.services.upload_validation import MAX_AUDIO_UPLOAD_BYTES
from apps.player.spotify import canonicalize_spotify_track_url


def download_spotify_track(
    spotify_url: str,
    output_dir: Path,
    *,
    command_runner: CommandRunner,
    metadata_client: TrackMetadataClient,
) -> Path:
    """Resolve metadata, then download one bounded YouTube audio search result.

    Raises:
        RuntimeError: No complete, bounded MP3 was produced.

    """
    canonical = canonicalize_spotify_track_url(spotify_url)
    metadata = metadata_client.get_track(canonical.rsplit("/", 1)[-1])
    query = f"ytsearch1:{' '.join(metadata.artists)} {metadata.title} official audio"
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
        "--socket-timeout",
        "15",
        "--retries",
        "1",
        "--max-filesize",
        str(MAX_AUDIO_UPLOAD_BYTES),
        "--match-filter",
        "duration <= 900 & !is_live",
        "--extract-audio",
        "--audio-format",
        "mp3",
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
        if (
            result.returncode == 0
            and output.is_file()
            and 0 < output.stat().st_size <= MAX_AUDIO_UPLOAD_BYTES
        ):
            return output
    output.unlink(missing_ok=True)
    if timed_out:
        raise RuntimeError("Download timed out. Please retry.")
    raise RuntimeError("Download failed. Please retry.")
