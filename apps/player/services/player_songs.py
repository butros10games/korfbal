"""Application commands for the player-song lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Protocol

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
from apps.player.services.player_song_queries import (
    owned_player_song_or_none,
    player_song_by_id,
)
from apps.player.spotify import canonicalize_spotify_track_url


logger = logging.getLogger(__name__)

CELERY_BROKER_UNAVAILABLE_MESSAGE = "Celery broker unavailable"


class PlayerSongNotFoundError(Exception):
    """Raised when a player does not own the requested song."""


class PlayerSongAlreadyReadyError(Exception):
    """Raised when a ready song is submitted for retry."""


class GoalSongClipPreparer(Protocol):
    """Prepare or retrieve one immutable audio clip."""

    def __call__(
        self,
        *,
        audio_file: FieldFile,
        song: PlayerSong,
        start_seconds: int,
        duration_seconds: int,
    ) -> str | None:
        """Return the clip storage key, or None when it cannot be prepared."""


@dataclass(frozen=True, slots=True)
class PlayerSongCreation:
    """Result of an idempotent player-song creation command."""

    song: PlayerSong
    created: bool


@dataclass(frozen=True, slots=True)
class PlayerSongSettingsPatch:
    """Validated optional settings for one player song."""

    start_time_seconds: int | None = None
    playback_speed: float | None = None


@dataclass(frozen=True, slots=True)
class PlayerSongClipRequest:
    """Normalized parameters for resolving one player-song clip."""

    song_id: str
    start_seconds: int
    duration_seconds: int
    enqueue_if_missing: bool


@dataclass(frozen=True, slots=True)
class PlayerSongClip:
    """Resolved source and optional prepared clip for one song."""

    song: PlayerSong
    audio_file: FieldFile
    clip_key: str | None


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


def enqueue_download_for_player_song(
    song: PlayerSong,
    *,
    jobs: SongDownloadDispatcher,
) -> None:
    """Dispatch work for the song's effective audio source immediately."""
    cached = song.cached_song
    if cached is not None:
        jobs.cached_song(str(cached.id_uuid))
        return

    jobs.player_song(str(song.id_uuid))


def _dispatch_download(
    song: PlayerSong,
    *,
    jobs: SongDownloadDispatcher,
    operation: str,
    mark_unavailable: bool,
) -> None:
    try:
        enqueue_download_for_player_song(song, jobs=jobs)
    except JobDispatchUnavailableError:
        logger.warning(
            "Celery broker unavailable; could not %s PlayerSong %s",
            operation,
            song.id_uuid,
            exc_info=True,
        )
        if mark_unavailable:
            _mark_broker_unavailable(song)


def _dispatch_download_after_commit(
    song: PlayerSong,
    *,
    jobs: SongDownloadDispatcher,
    operation: str,
    mark_unavailable: bool = False,
) -> None:
    """Schedule external work only after the database state commits."""

    def dispatch() -> None:
        _dispatch_download(
            song,
            jobs=jobs,
            operation=operation,
            mark_unavailable=mark_unavailable,
        )

    transaction.on_commit(dispatch)


@transaction.atomic
def create_player_song(
    *,
    player: Player,
    uploaded_audio: UploadedFile | None,
    spotify_url: str | None,
    jobs: SongDownloadDispatcher,
) -> PlayerSongCreation:
    """Create a player song and dispatch processing after commit."""
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
        _dispatch_download_after_commit(
            song,
            jobs=jobs,
            operation="prepare",
        )
        return PlayerSongCreation(song=song, created=True)

    canonical_url = canonicalize_spotify_track_url(str(spotify_url or "").strip())
    cached, _ = CachedSong.objects.get_or_create(spotify_url=canonical_url)
    song, created = PlayerSong.objects.get_or_create(
        player=player,
        cached_song=cached,
        defaults={"spotify_url": canonical_url},
    )
    _dispatch_download_after_commit(
        song,
        jobs=jobs,
        operation="enqueue",
        mark_unavailable=True,
    )
    return PlayerSongCreation(song=song, created=created)


def _owned_song_for_update(*, player: Player, song_id: str) -> PlayerSong:
    song = owned_player_song_or_none(
        player=player,
        song_id=song_id,
        for_update=True,
    )
    if song is None:
        raise PlayerSongNotFoundError
    return song


def _lock_player(player: Player) -> Player:
    """Lock the profile row that owns goal-song selection state."""
    return Player.objects.select_for_update().get(pk=player.pk)


@transaction.atomic
def update_owned_player_song_settings(
    *,
    player: Player,
    song_id: str,
    settings: PlayerSongSettingsPatch,
    jobs: SongDownloadDispatcher,
) -> PlayerSong:
    """Update an owned song and keep the selected legacy start time in sync."""
    locked_player = _lock_player(player)
    song = _owned_song_for_update(player=locked_player, song_id=song_id)
    update_fields: list[str] = ["updated_at"]

    if settings.start_time_seconds is not None:
        song.start_time_seconds = settings.start_time_seconds
        update_fields.append("start_time_seconds")
    if settings.playback_speed is not None:
        song.playback_speed = settings.playback_speed
        update_fields.append("playback_speed")

    song.save(update_fields=update_fields)
    selected_ids = [
        value for value in (locked_player.goal_song_song_ids or []) if value
    ]
    if settings.start_time_seconds is not None and selected_ids[:1] == [
        str(song.id_uuid)
    ]:
        locked_player.song_start_time = song.start_time_seconds
        locked_player.save(update_fields=["song_start_time"])

    if settings.start_time_seconds is not None:
        _dispatch_download_after_commit(
            song,
            jobs=jobs,
            operation="re-prepare",
        )
    return song


@transaction.atomic
def delete_owned_player_song(*, player: Player, song_id: str) -> None:
    """Delete an owned song and repair the player's goal-song selection."""
    locked_player = _lock_player(player)
    song = _owned_song_for_update(player=locked_player, song_id=song_id)
    remove_deleted_song_from_goal_song_selection(
        player=locked_player,
        deleted_song_id=str(song.id_uuid),
    )
    song.delete()


@transaction.atomic
def retry_owned_player_song_download(
    *,
    player: Player,
    song_id: str,
    jobs: SongDownloadDispatcher,
) -> PlayerSong:
    """Reset and re-dispatch a non-ready song owned by a player.

    Raises:
        PlayerSongAlreadyReadyError: If the effective song source is already ready.

    """
    song = _owned_song_for_update(player=player, song_id=song_id)
    if song.effective_status == PlayerSongStatus.READY:
        raise PlayerSongAlreadyReadyError

    cached = song.cached_song
    if cached is not None:
        cached = CachedSong.objects.select_for_update().get(pk=cached.pk)
        song.cached_song = cached
        cached.status = CachedSongStatus.QUEUED
        cached.error_message = ""
        cached.save(update_fields=["status", "error_message", "updated_at"])
    else:
        song.status = PlayerSongStatus.QUEUED
        song.error_message = ""
        song.save(update_fields=["status", "error_message", "updated_at"])

    _dispatch_download_after_commit(
        song,
        jobs=jobs,
        operation="retry",
        mark_unavailable=True,
    )
    return song


def resolve_player_song_clip(
    *,
    request: PlayerSongClipRequest,
    prepare_clip: GoalSongClipPreparer,
    jobs: SongDownloadDispatcher,
) -> PlayerSongClip | None:
    """Resolve and prepare a public clip without exposing ORM work to HTTP views."""
    song = player_song_by_id(request.song_id)
    if song is None or not song.effective_audio_file:
        return None

    audio_file = song.effective_audio_file
    clip_key = prepare_clip(
        audio_file=audio_file,
        song=song,
        start_seconds=request.start_seconds,
        duration_seconds=request.duration_seconds,
    )
    if request.enqueue_if_missing and clip_key is None:
        _dispatch_download(
            song,
            jobs=jobs,
            operation="prepare",
            mark_unavailable=False,
        )
    return PlayerSongClip(song=song, audio_file=audio_file, clip_key=clip_key)
