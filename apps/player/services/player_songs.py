"""Song creation, update, and retry helpers for player API endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models.fields.files import FieldFile
from kombu.exceptions import OperationalError as KombuOperationalError

from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.spotify import canonicalize_spotify_track_url
from apps.player.tasks import download_cached_song, download_player_song


logger = logging.getLogger(__name__)

CELERY_BROKER_UNAVAILABLE_MESSAGE = "Celery broker unavailable"


def effective_song_audio_file(song: PlayerSong) -> FieldFile:
    """Return the effective audio file for a PlayerSong."""
    return (
        song.cached_song.audio_file if song.cached_song is not None else song.audio_file
    )


def effective_song_status(song: PlayerSong) -> str:
    """Return the effective status for a PlayerSong."""
    return song.cached_song.status if song.cached_song is not None else song.status


def _should_run_tasks_eagerly() -> bool:
    return bool(
        getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
        or getattr(settings, "TESTING", False)
    )


def _mark_broker_unavailable(song: PlayerSong) -> None:
    cached = song.cached_song
    if cached is not None:
        cached.status = CachedSongStatus.FAILED
        cached.error_message = CELERY_BROKER_UNAVAILABLE_MESSAGE
        cached.save(update_fields=["status", "error_message", "updated_at"])
        return

    song.status = PlayerSongStatus.FAILED
    song.error_message = CELERY_BROKER_UNAVAILABLE_MESSAGE
    song.save(update_fields=["status", "error_message", "updated_at"])


def create_player_song(
    *,
    player: Player,
    uploaded_audio: UploadedFile | None,
    spotify_url: str | None,
) -> tuple[PlayerSong, bool]:
    """Create a player song from an upload or queue a Spotify download."""
    if isinstance(uploaded_audio, UploadedFile):
        filename = Path(uploaded_audio.name or "uploaded.mp3").name
        title = Path(filename).stem[:255]

        song = PlayerSong.objects.create(
            player=player,
            cached_song=None,
            spotify_url="",
            title=title,
            artists="",
            duration_seconds=None,
            start_time_seconds=0,
            playback_speed=1.0,
            status=PlayerSongStatus.READY,
            error_message="",
            audio_file=uploaded_audio,
        )
        return song, True

    canonical_url = canonicalize_spotify_track_url(str(spotify_url or "").strip())
    cached, _ = CachedSong.objects.get_or_create(spotify_url=canonical_url)
    song, created = PlayerSong.objects.get_or_create(
        player=player,
        cached_song=cached,
        defaults={"spotify_url": canonical_url},
    )

    try:
        if _should_run_tasks_eagerly():
            download_cached_song.apply(args=[str(cached.id_uuid)])
        else:
            download_cached_song.delay(str(cached.id_uuid))
    except KombuOperationalError:
        logger.warning(
            "Celery broker unavailable; could not enqueue PlayerSong %s",
            song.id_uuid,
            exc_info=True,
        )
        _mark_broker_unavailable(song)

    return song, created


def update_player_song_settings(
    *,
    song: PlayerSong,
    start_time_seconds: int | None = None,
    playback_speed: float | None = None,
) -> None:
    """Persist playback settings for a PlayerSong."""
    update_fields: list[str] = ["updated_at"]
    if start_time_seconds is not None:
        song.start_time_seconds = int(start_time_seconds)
        update_fields.append("start_time_seconds")
    if playback_speed is not None:
        song.playback_speed = float(playback_speed)
        update_fields.append("playback_speed")

    song.save(update_fields=update_fields)


def enqueue_download_for_player_song(song: PlayerSong) -> None:
    """Enqueue or eagerly execute the download task for a PlayerSong."""
    cached = song.cached_song
    if _should_run_tasks_eagerly():
        if cached is not None:
            download_cached_song.apply(args=[str(cached.id_uuid)])
        else:
            download_player_song.apply(args=[str(song.id_uuid)])
        return

    if cached is not None:
        download_cached_song.delay(str(cached.id_uuid))
        return

    download_player_song.delay(str(song.id_uuid))


def retry_player_song_download(song: PlayerSong) -> None:
    """Reset a failed song back to queued and re-enqueue its download."""
    cached = song.cached_song
    with transaction.atomic():
        if cached is not None:
            cached.status = CachedSongStatus.QUEUED
            cached.error_message = ""
            cached.save(update_fields=["status", "error_message", "updated_at"])
        else:
            song.status = PlayerSongStatus.QUEUED
            song.error_message = ""
            song.save(update_fields=["status", "error_message", "updated_at"])

    try:
        enqueue_download_for_player_song(song)
    except KombuOperationalError:
        logger.warning(
            "Celery broker unavailable; could not retry PlayerSong %s",
            song.id_uuid,
            exc_info=True,
        )
        _mark_broker_unavailable(song)
