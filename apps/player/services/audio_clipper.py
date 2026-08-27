"""Audio clip generation helpers.

This module centralizes ffmpeg invocation details so API endpoints don't
duplicate (and drift) the transcoding flags.

All helpers are best-effort: callers are expected to fall back to the original
audio when ffmpeg is unavailable or fails.
"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
from typing import Final

from apps.player.application.ports import CommandRunner, CommandRunOptions


_FFMPEG_DEFAULT_QUALITY: Final[str] = "4"


@dataclass(frozen=True, slots=True)
class Mp3ClipSpec:
    """Time range for an MP3 clip."""

    start_seconds: int = 0
    duration_seconds: int = 8


def find_ffmpeg() -> str | None:
    """Return the resolved ffmpeg path (or None when not installed)."""
    return shutil.which("ffmpeg")


def transcode_to_mp3_clip_file(
    *,
    input_path: str,
    output_path: str,
    spec: Mp3ClipSpec | None = None,
    ffmpeg_path: str | None = None,
    command_runner: CommandRunner,
) -> None:
    """Transcode an input audio file into a short MP3 clip.

    Raises:
        FileNotFoundError: when ffmpeg is not available.

    """
    resolved = ffmpeg_path or find_ffmpeg()
    if not resolved:
        raise FileNotFoundError("ffmpeg not found")
    clip = spec or Mp3ClipSpec()

    cmd = [
        resolved,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(max(0, clip.start_seconds)),
        "-i",
        input_path,
        "-t",
        str(max(1, clip.duration_seconds)),
        "-vn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-acodec",
        "libmp3lame",
        "-q:a",
        _FFMPEG_DEFAULT_QUALITY,
        output_path,
    ]

    command_runner.run(cmd, CommandRunOptions(check=True))
