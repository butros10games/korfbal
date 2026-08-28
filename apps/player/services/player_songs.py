"""Song creation, update, and retry helpers for player API endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models.fields.files import FieldFile

from apps.player.application.ports import (
    JobDispatchUnavailableError,
    SongDownloadDispatcher,
)
from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.goal_song import remove_deleted_song_from_goal_song_selection
from apps.player.spotify import canonicalize_spotify_track_url


logger = logging.getLogger(__name__)

CELERY_BROKER_UNAVAILABLE_MESSAGE = "Celery broker unavailable"


class PlayerSongNotFoundError(Exception):
    """Raised when a player does not own the requested song."""


class PlayerSongAlreadyReadyError(Exception):
    """Raised when a ready song is submitted for retry."""


def effective_song_audio_file(song: PlayerSong) -> FieldFile:
    """Return the effective audio file for a PlayerSong."""
    return (
        song.cached_song.audio_file if song.cached_song is not None else song.audio_file
    )


def effective_song_status(song: PlayerSong) -> str:
    """Return the effective status for a PlayerSong."""
    return song.cached_song.status if song.cached_song is not None else song.status


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
    jobs: SongDownloadDispatcher,
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
        try:
            enqueue_download_for_player_song(song, jobs=jobs)
        except JobDispatchUnavailableError:
            logger.warning(
                "Celery broker unavailable; could not prepare PlayerSong %s",
                song.id_uuid,
                exc_info=True,
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
        jobs.cached_song(str(cached.id_uuid))
    except JobDispatchUnavailableError:
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
    jobs: SongDownloadDispatcher,
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
    try:
        enqueue_download_for_player_song(song, jobs=jobs)
    except JobDispatchUnavailableError:
        logger.warning(
            "Celery broker unavailable; could not re-prepare PlayerSong %s",
            song.id_uuid,
            exc_info=True,
        )


def enqueue_download_for_player_song(
    song: PlayerSong, *, jobs: SongDownloadDispatcher
) -> None:
    """Enqueue download or clip preparation for a PlayerSong."""
    cached = song.cached_song
    if cached is not None:
        jobs.cached_song(str(cached.id_uuid))
        return

    jobs.player_song(str(song.id_uuid))


def retry_player_song_download(
    song: PlayerSong, *, jobs: SongDownloadDispatcher
) -> None:
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
        enqueue_download_for_player_song(song, jobs=jobs)
    except JobDispatchUnavailableError:
        logger.warning(
            "Celery broker unavailable; could not retry PlayerSong %s",
            song.id_uuid,
            exc_info=True,
        )
        _mark_broker_unavailable(song)


def owned_player_song(
    *,
    player: Player,
    song_id: str,
) -> PlayerSong:
    """Return a song owned by a player.

    Raises:
        PlayerSongNotFoundError: If the player does not own the requested song.

    """
    song = (
        PlayerSong.objects
        .select_related("cached_song")
        .filter(player=player, id_uuid=song_id)
        .first()
    )
    if song is None:
        raise PlayerSongNotFoundError
    return song


@transaction.atomic
def delete_owned_player_song(*, player: Player, song_id: str) -> None:
    """Delete an owned song and repair the player's goal-song selection."""
    song = owned_player_song(player=player, song_id=song_id)
    remove_deleted_song_from_goal_song_selection(
        player=player,
        deleted_song_id=str(song.id_uuid),
    )
    song.delete()


def retry_owned_player_song_download(
    *,
    player: Player,
    song_id: str,
    jobs: SongDownloadDispatcher,
) -> PlayerSong:
    """Retry a non-ready song owned by a player.

    Raises:
        PlayerSongAlreadyReadyError: If the song is already ready.

    """
    song = owned_player_song(player=player, song_id=song_id)
    if effective_song_status(song) == PlayerSongStatus.READY:
        raise PlayerSongAlreadyReadyError
    retry_player_song_download(song, jobs=jobs)
    return song
