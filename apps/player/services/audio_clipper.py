"""Audio clip generation helpers.

This module centralizes ffmpeg invocation details so API endpoints don't
duplicate (and drift) the transcoding flags.

All helpers are best-effort: callers are expected to fall back to the original
audio when ffmpeg is unavailable or fails.
"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess  # nosec B404
from typing import Final


_FFMPEG_DEFAULT_QUALITY: Final[str] = "4"


@dataclass(frozen=True, slots=True)
class Mp3ClipSpec:
    """Parameters for generating an MP3 clip."""

    start_seconds: int = 0
    duration_seconds: int = 8
    quality: str = _FFMPEG_DEFAULT_QUALITY


def find_ffmpeg() -> str | None:
    """Return the resolved ffmpeg path (or None when not installed)."""
    return shutil.which("ffmpeg")


def build_ffmpeg_mp3_clip_command(
    *,
    ffmpeg_path: str,
    input_path: str,
    output_path: str,
    spec: Mp3ClipSpec,
) -> list[str]:
    """Build an ffmpeg command that produces a short MP3 clip.

    Notes:
        - Strips metadata/chapters to avoid inheriting TLEN (full-track length)
          and confusing duration reporting for short clips.
        - Uses libmp3lame VBR quality mode.

    """
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(max(0, spec.start_seconds)),
        "-i",
        input_path,
        "-t",
        str(max(1, spec.duration_seconds)),
        "-vn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-acodec",
        "libmp3lame",
        "-q:a",
        str(spec.quality),
        output_path,
    ]


def transcode_to_mp3_clip_file(
    *,
    input_path: str,
    output_path: str,
    spec: Mp3ClipSpec | None = None,
    ffmpeg_path: str | None = None,
) -> None:
    """Transcode an input audio file into a short MP3 clip.

    Raises:
        FileNotFoundError: when ffmpeg is not available.

    """
    resolved = ffmpeg_path or find_ffmpeg()
    if not resolved:
        raise FileNotFoundError("ffmpeg not found")

    spec = spec or Mp3ClipSpec()

    cmd = build_ffmpeg_mp3_clip_command(
        ffmpeg_path=resolved,
        input_path=input_path,
        output_path=output_path,
        spec=spec,
    )

    subprocess.run(cmd, check=True)  # nosec B603
