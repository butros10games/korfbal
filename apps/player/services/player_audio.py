"""Audio-file helpers for player API endpoints."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess  # nosec B404
import tempfile
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.player.models.player_song import PlayerSong
from apps.player.services.audio_clipper import (
    Mp3ClipSpec,
    find_ffmpeg,
    transcode_to_mp3_clip_file,
)


logger = logging.getLogger(__name__)


def clip_or_full_location(
    *,
    audio_file: Any,
    song: PlayerSong,
    start_seconds: int,
    duration_seconds: int,
) -> str:
    """Return a deterministic clip URL, falling back to the full audio URL."""
    clip_key = (
        f"song_clips_v2/{song.id_uuid}/start_{start_seconds}_dur_{duration_seconds}.mp3"
    )

    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return str(audio_file.url)

    try:
        if default_storage.exists(clip_key):
            return default_storage.url(clip_key)

        with tempfile.TemporaryDirectory(prefix="song_clip_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_path = tmpdir_path / "input"
            output_path = tmpdir_path / "clip.mp3"

            with audio_file.open("rb") as source, input_path.open("wb") as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)

            transcode_to_mp3_clip_file(
                input_path=str(input_path),
                output_path=str(output_path),
                spec=Mp3ClipSpec(
                    start_seconds=start_seconds,
                    duration_seconds=duration_seconds,
                ),
                ffmpeg_path=ffmpeg_path,
            )

            clip_bytes = output_path.read_bytes()
            default_storage.save(
                clip_key,
                ContentFile(clip_bytes),
            )
        return default_storage.url(clip_key)
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.info(
            "Clip generation failed; falling back to full audio for %s",
            song.id_uuid,
            exc_info=True,
        )
        return str(audio_file.url)
    except Exception:
        logger.warning(
            "Unexpected error generating clip; falling back to full audio for %s",
            song.id_uuid,
            exc_info=True,
        )
        return str(audio_file.url)
