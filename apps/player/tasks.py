"""Celery tasks for the player app."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess  # nosec B404 - required for invoking spotDL CLI safely (shell=False + input validation)
import tempfile
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.files import File
from django.db import transaction

from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.spotify import canonicalize_spotify_track_url


logger = logging.getLogger(__name__)


def _validate_spotify_url_for_spotdl(spotify_url: str) -> None:
    """Validate Spotify URL before passing it to a subprocess.

    We intentionally run the spotDL CLI via subprocess (shell=False) but still
    treat user-provided URLs as untrusted input.

    Accepted formats:
    - https://open.spotify.com/track/<id>
    - https://open.spotify.com/intl-xx/track/<id>

    Raises:
        ValueError: if the URL format is not an accepted Spotify track URL.

    """
    min_printable_ascii = 32

    value = (spotify_url or "").strip()
    if not value:
        raise ValueError("Spotify URL is required")

    # Avoid control characters / whitespace that could create surprises in logs or
    # tooling.
    if any(ch.isspace() for ch in value) or any(
        ord(ch) < min_printable_ascii for ch in value
    ):
        raise ValueError("Spotify URL contains invalid whitespace/control characters")

    # Ensure it is a canonical track URL (also validates structure).
    _ = canonicalize_spotify_track_url(value)


def _pick_downloaded_audio(output_dir: Path) -> Path:
    """Pick the most likely downloaded audio file.

    Raises:
        FileNotFoundError: When spotDL produces no audio file.

    """
    candidates: list[Path] = []
    for pattern in ("*.mp3", "*.m4a", "*.opus", "*.ogg", "*.wav", "*.flac"):
        candidates.extend(output_dir.rglob(pattern))

    if not candidates:
        raise FileNotFoundError("spotDL finished but no audio file was produced")

    # Prefer the largest file (often the actual audio).
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


def _run_spotdl(spotify_url: str, output_dir: Path) -> Path:
    """Download a Spotify link using spotDL into the given directory.

    Raises:
        RuntimeError: When spotDL exits non-zero.

    """
    _validate_spotify_url_for_spotdl(spotify_url)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / "{title}.{output-ext}")

    attempted: list[tuple[list[str], str]] = []
    commands: list[list[str]] = [
        [
            "spotdl",
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
            "spotdl",
            spotify_url,
            "--output",
            output_template,
            "--format",
            "mp3",
            "--threads",
            "1",
        ],
    ]

    last_error = ""
    for cmd in commands:
        logger.info("Running spotDL: %s", " ".join(cmd))
        proc = subprocess.run(  # nosec B603 - shell=False with validated URL; command is fixed argv list
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60 * 15,
            shell=False,
        )
        attempted.append((cmd, proc.stdout[-2000:]))
        if proc.returncode == 0:
            return _pick_downloaded_audio(output_dir)
        last_error = (proc.stderr or proc.stdout or "").strip()[-4000:]

    details = "\n".join(" ".join(cmd) for cmd, _ in attempted)
    raise RuntimeError(f"spotDL failed. Tried: {details}\n{last_error}")


@shared_task(bind=True)
def download_cached_song(self: Any, cached_song_id: str) -> None:  # noqa: ANN401
    """Download a cached song (from a Spotify URL) and upload it to storage."""
    cached = CachedSong.objects.filter(id_uuid=cached_song_id).first()
    if cached is None:
        logger.warning("CachedSong %s not found", cached_song_id)
        return

    if cached.status == CachedSongStatus.READY and cached.audio_file:
        return

    # Avoid duplicate work if another worker is already processing it.
    if cached.status in {CachedSongStatus.DOWNLOADING, CachedSongStatus.UPLOADING}:
        return

    try:
        with transaction.atomic():
            locked = (
                CachedSong.objects.select_for_update()
                .filter(id_uuid=cached_song_id)
                .first()
            )
            if locked is None:
                return
            if locked.status == CachedSongStatus.READY and locked.audio_file:
                return
            if locked.status in {
                CachedSongStatus.DOWNLOADING,
                CachedSongStatus.UPLOADING,
            }:
                return
            locked.status = CachedSongStatus.DOWNLOADING
            locked.error_message = ""
            locked.save(update_fields=["status", "error_message", "updated_at"])

        with tempfile.TemporaryDirectory(prefix="spotdl_") as tmp:
            output_dir = Path(tmp)
            if getattr(settings, "TESTING", False):
                downloaded = output_dir / "dummy.mp3"
                downloaded.write_bytes(b"ID3")
            else:
                downloaded = _run_spotdl(cached.spotify_url, output_dir)

            with transaction.atomic():
                cached.status = CachedSongStatus.UPLOADING
                cached.save(update_fields=["status", "updated_at"])

            suffix = downloaded.suffix or ".mp3"
            target_name = f"{cached.id_uuid}{suffix}"
            with downloaded.open("rb") as handle:
                cached.audio_file.save(target_name, File(handle), save=False)

            with transaction.atomic():
                cached.status = CachedSongStatus.READY
                cached.error_message = ""
                cached.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "audio_file",
                        "updated_at",
                    ]
                )

    except Exception as exc:
        logger.exception("Failed to download CachedSong %s", cached_song_id)
        cached.status = CachedSongStatus.FAILED
        cached.error_message = str(exc)
        cached.save(update_fields=["status", "error_message", "updated_at"])
        raise


@shared_task(bind=True)
def download_player_song(self: Any, song_id: str) -> None:  # noqa: ANN401
    """Backward compatible wrapper.

    Historically we queued downloads per PlayerSong. Now PlayerSong can point to a
    shared CachedSong. This wrapper resolves the cached song and queues it.
    """
    song = (
        PlayerSong.objects.select_related("cached_song").filter(id_uuid=song_id).first()
    )
    if song is None:
        logger.warning("PlayerSong %s not found", song_id)
        return

    if song.cached_song is not None:
        download_cached_song.apply(args=[str(song.cached_song.id_uuid)])
        return

    # Legacy: if this PlayerSong still stores its own audio, keep the old behavior.
    if song.status == PlayerSongStatus.READY and song.audio_file:
        return

    try:
        with transaction.atomic():
            song.status = PlayerSongStatus.DOWNLOADING
            song.error_message = ""
            song.save(update_fields=["status", "error_message", "updated_at"])

        with tempfile.TemporaryDirectory(prefix="spotdl_") as tmp:
            output_dir = Path(tmp)
            if getattr(settings, "TESTING", False):
                downloaded = output_dir / "dummy.mp3"
                downloaded.write_bytes(b"ID3")
            else:
                downloaded = _run_spotdl(song.spotify_url, output_dir)

            with transaction.atomic():
                song.status = PlayerSongStatus.UPLOADING
                song.save(update_fields=["status", "updated_at"])

            suffix = downloaded.suffix or ".mp3"
            target_name = f"{song.player.id_uuid}/{song.id_uuid}{suffix}"
            with downloaded.open("rb") as handle:
                song.audio_file.save(target_name, File(handle), save=False)

            with transaction.atomic():
                song.status = PlayerSongStatus.READY
                song.error_message = ""
                song.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "audio_file",
                        "updated_at",
                    ]
                )

    except Exception as exc:
        logger.exception("Failed to download PlayerSong %s", song_id)
        song.status = PlayerSongStatus.FAILED
        song.error_message = str(exc)
        song.save(update_fields=["status", "error_message", "updated_at"])
        raise
