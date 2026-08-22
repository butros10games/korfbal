"""Audio-file helpers for player API endpoints."""

from __future__ import annotations

import hashlib
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

GOAL_SONG_CLIP_DURATION_SECONDS = 8


def goal_song_clip_key(
    *,
    audio_file: Any,
    song: PlayerSong,
    start_seconds: int,
    duration_seconds: int = GOAL_SONG_CLIP_DURATION_SECONDS,
) -> str:
    """Return the immutable storage key for a prepared goal-song clip."""
    source_name = str(getattr(audio_file, "name", "") or "unknown")
    source_revision = hashlib.sha256(source_name.encode()).hexdigest()[:12]
    return (
        f"song_clips_v3/{song.id_uuid}/"
        f"{source_revision}_start_{max(0, start_seconds)}_"
        f"dur_{max(1, duration_seconds)}.mp3"
    )


def ensure_goal_song_clip(
    *,
    audio_file: Any,
    song: PlayerSong,
    start_seconds: int,
    duration_seconds: int = GOAL_SONG_CLIP_DURATION_SECONDS,
) -> str | None:
    """Materialize a short goal-song clip and return its storage key.

    This helper is intentionally safe to call from upload/download and settings
    workflows. The playback endpoint retains a full-track fallback for legacy
    songs whose clip has not been prepared or when ffmpeg is unavailable.
    """
    clip_key = goal_song_clip_key(
        audio_file=audio_file,
        song=song,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
    )

    if default_storage.exists(clip_key):
        return clip_key

    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="song_clip_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_path = tmpdir_path / "input"
            output_path = tmpdir_path / "clip.mp3"

            with audio_file.open("rb") as source, input_path.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
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

            saved_key = default_storage.save(
                clip_key,
                ContentFile(output_path.read_bytes()),
            )
            return str(saved_key)
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.info(
            "Goal-song clip generation failed for %s",
            song.id_uuid,
            exc_info=True,
        )
    except Exception:
        logger.warning(
            "Unexpected goal-song clip generation failure for %s",
            song.id_uuid,
            exc_info=True,
        )
    return None


def prepare_player_song_clip(song: PlayerSong) -> str | None:
    """Prepare the standard tracker clip for a ready PlayerSong."""
    audio_file = (
        song.cached_song.audio_file if song.cached_song is not None else song.audio_file
    )
    if not audio_file:
        return None
    return ensure_goal_song_clip(
        audio_file=audio_file,
        song=song,
        start_seconds=max(0, int(song.start_time_seconds or 0)),
    )


def clip_or_full_location(
    *,
    audio_file: Any,
    song: PlayerSong,
    start_seconds: int,
    duration_seconds: int,
) -> str:
    """Return a deterministic clip URL, falling back to the full audio URL."""
    clip_key = ensure_goal_song_clip(
        audio_file=audio_file,
        song=song,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
    )
    return default_storage.url(clip_key) if clip_key else str(audio_file.url)
