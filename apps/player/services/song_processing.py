"""Application services for downloading and preparing player songs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
import logging
from pathlib import Path
import tempfile

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.player_song_queries import (
    player_song_by_id,
    player_song_queryset,
)


logger = logging.getLogger(__name__)
TrackDownloader = Callable[[str, Path], Path]
ClipPreparer = Callable[[PlayerSong], str | None]
CachedSongDispatcher = Callable[[str], None]


def _prepare_cached_song_clips(
    cached: CachedSong,
    *,
    prepare_clip: ClipPreparer,
) -> None:
    songs = player_song_queryset().filter(cached_song=cached)
    for song in songs:
        prepare_clip(song)


def _cached_song_is_ready(cached: CachedSong) -> bool:
    return cached.status == CachedSongStatus.READY and bool(cached.audio_file)


def _cached_song_in_progress_is_not_stale(
    *,
    cached: CachedSong,
    now: datetime,
    stale_in_progress_seconds: int,
) -> bool:
    if cached.status not in {
        CachedSongStatus.DOWNLOADING,
        CachedSongStatus.UPLOADING,
    }:
        return False

    age_seconds = (now - cached.updated_at).total_seconds()
    if age_seconds < stale_in_progress_seconds:
        return True

    logger.warning(
        "CachedSong %s appears stuck in %s for %.0fs; reclaiming",
        cached.id_uuid,
        cached.status,
        age_seconds,
    )
    return False


def _lock_cached_song_for_download(
    *,
    cached_song_id: str,
    now: datetime,
    stale_in_progress_seconds: int,
) -> CachedSong | None:
    locked = (
        CachedSong.objects.select_for_update().filter(id_uuid=cached_song_id).first()
    )
    if locked is None or _cached_song_is_ready(locked):
        return None

    if locked.audio_file and locked.status != CachedSongStatus.READY:
        locked.status = CachedSongStatus.READY
        locked.error_message = ""
        locked.save(update_fields=["status", "error_message", "updated_at"])
        return None

    if _cached_song_in_progress_is_not_stale(
        cached=locked,
        now=now,
        stale_in_progress_seconds=stale_in_progress_seconds,
    ):
        return None

    locked.status = CachedSongStatus.DOWNLOADING
    locked.error_message = ""
    locked.save(update_fields=["status", "error_message", "updated_at"])
    return locked


@contextmanager
def _downloaded_track(
    spotify_url: str,
    *,
    download_track: TrackDownloader,
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="spotdl_") as temporary_directory:
        output_dir = Path(temporary_directory)
        if getattr(settings, "TESTING", False):
            downloaded = output_dir / "dummy.mp3"
            downloaded.write_bytes(b"ID3")
        else:
            downloaded = download_track(spotify_url, output_dir)
        yield downloaded


def _download_and_store_cached_song(
    cached: CachedSong,
    *,
    download_track: TrackDownloader,
    prepare_clip: ClipPreparer,
) -> None:
    with _downloaded_track(
        cached.spotify_url,
        download_track=download_track,
    ) as downloaded:
        with transaction.atomic():
            cached.status = CachedSongStatus.UPLOADING
            cached.save(update_fields=["status", "updated_at"])

        target_name = f"{cached.id_uuid}{downloaded.suffix or '.mp3'}"
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
        _prepare_cached_song_clips(cached, prepare_clip=prepare_clip)


def _download_and_store_player_song(
    song: PlayerSong,
    *,
    download_track: TrackDownloader,
    prepare_clip: ClipPreparer,
) -> None:
    with _downloaded_track(
        song.spotify_url,
        download_track=download_track,
    ) as downloaded:
        with transaction.atomic():
            song.status = PlayerSongStatus.UPLOADING
            song.save(update_fields=["status", "updated_at"])

        target_name = (
            f"{song.player.id_uuid}/{song.id_uuid}{downloaded.suffix or '.mp3'}"
        )
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
        prepare_clip(song)


def process_cached_song_download(
    cached_song_id: str,
    *,
    download_track: TrackDownloader,
    prepare_clip: ClipPreparer,
) -> None:
    """Download one shared song and prepare every dependent player clip."""
    cached = CachedSong.objects.filter(id_uuid=cached_song_id).first()
    if cached is None:
        logger.warning("CachedSong %s not found", cached_song_id)
        return

    timeout_seconds = int(getattr(settings, "SPOTDL_DOWNLOAD_TIMEOUT_SECONDS", 900))
    stale_in_progress_seconds = int(
        getattr(
            settings,
            "SPOTDL_STALE_IN_PROGRESS_SECONDS",
            timeout_seconds + 60,
        )
    )
    now = timezone.now()

    if _cached_song_is_ready(cached):
        _prepare_cached_song_clips(cached, prepare_clip=prepare_clip)
        return
    if _cached_song_in_progress_is_not_stale(
        cached=cached,
        now=now,
        stale_in_progress_seconds=stale_in_progress_seconds,
    ):
        return

    try:
        with transaction.atomic():
            locked = _lock_cached_song_for_download(
                cached_song_id=cached_song_id,
                now=now,
                stale_in_progress_seconds=stale_in_progress_seconds,
            )
            if locked is None:
                return
            cached = locked
        _download_and_store_cached_song(
            cached,
            download_track=download_track,
            prepare_clip=prepare_clip,
        )
    except Exception as exc:
        logger.exception("Failed to download CachedSong %s", cached_song_id)
        cached.status = CachedSongStatus.FAILED
        cached.error_message = str(exc)
        cached.save(update_fields=["status", "error_message", "updated_at"])
        raise


def process_player_song_download(
    song_id: str,
    *,
    dispatch_cached_song: CachedSongDispatcher,
    download_track: TrackDownloader,
    prepare_clip: ClipPreparer,
) -> None:
    """Prepare a player song through shared cache or legacy direct storage."""
    song = player_song_by_id(song_id)
    if song is None:
        logger.warning("PlayerSong %s not found", song_id)
        return

    if song.cached_song is not None:
        dispatch_cached_song(str(song.cached_song.id_uuid))
        return
    if song.status == PlayerSongStatus.READY and song.audio_file:
        prepare_clip(song)
        return

    try:
        with transaction.atomic():
            song.status = PlayerSongStatus.DOWNLOADING
            song.error_message = ""
            song.save(update_fields=["status", "error_message", "updated_at"])
        _download_and_store_player_song(
            song,
            download_track=download_track,
            prepare_clip=prepare_clip,
        )
    except Exception as exc:
        logger.exception("Failed to download PlayerSong %s", song_id)
        song.status = PlayerSongStatus.FAILED
        song.error_message = str(exc)
        song.save(update_fields=["status", "error_message", "updated_at"])
        raise
