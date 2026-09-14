"""Application commands for the player-song lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models.fields.files import FieldFile

from apps.player.application.ports import (
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
from apps.player.services.upload_validation import validate_audio_upload
from apps.player.song_sources import parse_song_source
from apps.team.models.team_data import TeamData


class PlayerSongNotFoundError(Exception):
    """Raised when a player does not own the requested song."""


MAX_CLIP_SECONDS = 15
MAX_SOURCE_SECONDS = 900
MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 2
MAX_CLIP_NAME_LENGTH = 80


class InvalidSongClipError(ValueError):
    """The clip is unavailable or its requested range is invalid."""


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
    clip_name: str | None = None
    clip_duration_seconds: int | None = None


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


def enqueue_download_for_player_song(
    song: PlayerSong, *, jobs: SongDownloadDispatcher
) -> None:
    """Record work atomically with the song, including clip-only updates."""
    jobs.player_song(str(song.id_uuid))


def create_player_song(
    *,
    player: Player,
    uploaded_audio: UploadedFile | None,
    spotify_url: str | None = None,
    source_url: str | None = None,
    jobs: SongDownloadDispatcher,
) -> PlayerSongCreation:
    """Create audio owned by a player's personal library."""
    return create_owned_song(
        owner=player,
        uploaded_audio=uploaded_audio,
        spotify_url=spotify_url,
        source_url=source_url,
        jobs=jobs,
    )


@transaction.atomic
def create_owned_song(
    *,
    owner: Player | TeamData,
    uploaded_audio: UploadedFile | None,
    spotify_url: str | None = None,
    source_url: str | None = None,
    jobs: SongDownloadDispatcher,
) -> PlayerSongCreation:
    """Create a player song and dispatch processing after commit."""
    # Serialize imports per owner: several clips may now reference one cache row.
    if isinstance(owner, Player):
        owner = Player.objects.select_for_update().get(pk=owner.pk)
    else:
        owner = TeamData.objects.select_for_update().get(pk=owner.pk)
    player = owner if isinstance(owner, Player) else None
    team_data = None if isinstance(owner, Player) else owner
    if isinstance(uploaded_audio, UploadedFile):
        validate_audio_upload(uploaded_audio)
        filename = Path(uploaded_audio.name or "uploaded.mp3").name
        title = Path(filename).stem[:255]

        song = PlayerSong.objects.create(
            player=player,
            team_data=team_data,
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
        enqueue_download_for_player_song(song, jobs=jobs)
        return PlayerSongCreation(song=song, created=True)

    source = parse_song_source(source_url or spotify_url or "")
    canonical_url = source.url
    cached, _ = CachedSong.objects.get_or_create(spotify_url=canonical_url)
    song = (
        PlayerSong.objects
        .filter(player=player, team_data=team_data, cached_song=cached)
        .order_by("created_at", "pk")
        .first()
    )
    created = song is None
    if song is None:
        song = PlayerSong.objects.create(
            player=player,
            team_data=team_data,
            cached_song=cached,
            spotify_url=canonical_url,
            start_time_seconds=source.start_seconds,
        )
    enqueue_download_for_player_song(song, jobs=jobs)
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
    validate_song_clip_settings(song, settings)
    update_fields: list[str] = ["updated_at"]

    if settings.start_time_seconds is not None:
        song.start_time_seconds = settings.start_time_seconds
        update_fields.append("start_time_seconds")
    if settings.playback_speed is not None:
        song.playback_speed = settings.playback_speed
        update_fields.append("playback_speed")

    for field in ("clip_name", "clip_duration_seconds"):
        value = getattr(settings, field)
        if value is not None:
            setattr(song, field, value)
            update_fields.append(field)
    song.save(update_fields=update_fields)
    selected_ids = [
        value for value in (locked_player.goal_song_song_ids or []) if value
    ]
    if settings.start_time_seconds is not None and selected_ids[:1] == [
        str(song.id_uuid)
    ]:
        locked_player.song_start_time = song.start_time_seconds
        locked_player.save(update_fields=["song_start_time"])

    if (
        settings.start_time_seconds is not None
        or settings.clip_duration_seconds is not None
    ):
        enqueue_download_for_player_song(song, jobs=jobs)
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
    """Reset and re-dispatch a non-ready song owned by a player."""
    song = _owned_song_for_update(player=player, song_id=song_id)
    return retry_song_download(song=song, jobs=jobs)


def retry_song_download(
    *, song: PlayerSong, jobs: SongDownloadDispatcher
) -> PlayerSong:
    """Retry a song after its caller has authorized and locked its owner.

    Raises:
        PlayerSongAlreadyReadyError: The song already has ready audio.

    """
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

    enqueue_download_for_player_song(song, jobs=jobs)
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
        enqueue_download_for_player_song(song, jobs=jobs)
    return PlayerSongClip(song=song, audio_file=audio_file, clip_key=clip_key)


def validate_song_clip_settings(
    song: PlayerSong, settings: PlayerSongSettingsPatch
) -> None:
    """Validate clip settings in the shared service, including alternate routes.

    Raises:
        InvalidSongClipError: A name, speed, duration or range is invalid.

    """
    start = (
        settings.start_time_seconds
        if settings.start_time_seconds is not None
        else song.start_time_seconds
    )
    duration = (
        settings.clip_duration_seconds
        if settings.clip_duration_seconds is not None
        else song.clip_duration_seconds
    )
    speed = (
        settings.playback_speed
        if settings.playback_speed is not None
        else song.playback_speed
    )
    name = settings.clip_name if settings.clip_name is not None else song.clip_name
    if (
        not 1 <= duration <= MAX_CLIP_SECONDS
        or not 0 <= start < MAX_SOURCE_SECONDS
        or not MIN_PLAYBACK_SPEED <= speed <= MAX_PLAYBACK_SPEED
        or len(name) > MAX_CLIP_NAME_LENGTH
    ):
        raise InvalidSongClipError(
            "Geef een geldige clipnaam, starttijd, lengte (1-15 sec.) en snelheid op."
        )
    full_duration = song.effective_duration_seconds
    if full_duration is not None and start + duration > full_duration:
        raise InvalidSongClipError(
            "De clip moet binnen de lengte van het nummer vallen."
        )


@transaction.atomic
def create_song_clip(
    *,
    owner: Player | TeamData,
    song_id: str,
    settings: PlayerSongSettingsPatch,
    jobs: SongDownloadDispatcher,
) -> PlayerSong:
    """Create an independent clip without downloading or copying owned audio.

    Uploaded clips share the same immutable owner-scoped file. Deleting a clip
    removes only its database selection; it must not delete its siblings' audio.

    Raises:
        PlayerSongNotFoundError: The source does not belong to this owner.
        InvalidSongClipError: The source is not ready or clip settings are invalid.

    """
    if isinstance(owner, Player):
        owner = Player.objects.select_for_update().get(pk=owner.pk)
        query = PlayerSong.objects.filter(player=owner)
    else:
        owner = TeamData.objects.select_for_update().get(pk=owner.pk)
        query = PlayerSong.objects.filter(team_data=owner)
    source = (
        query
        .select_related("cached_song")
        .select_for_update(of=("self",))
        .filter(pk=song_id)
        .first()
    )
    if source is None:
        raise PlayerSongNotFoundError
    if (
        source.effective_status != PlayerSongStatus.READY
        or not source.effective_audio_file
    ):
        raise InvalidSongClipError(
            "Wacht tot het nummer klaar is voordat je een clip toevoegt."
        )
    validate_song_clip_settings(source, settings)
    clip = PlayerSong.objects.create(
        player_id=source.player_id,
        team_data_id=source.team_data_id,
        cached_song=source.cached_song,
        spotify_url=source.spotify_url,
        audio_file=source.audio_file.name,
        title=source.title,
        artists=source.artists,
        duration_seconds=source.duration_seconds,
        status=source.status,
        start_time_seconds=settings.start_time_seconds
        if settings.start_time_seconds is not None
        else source.start_time_seconds,
        playback_speed=settings.playback_speed
        if settings.playback_speed is not None
        else source.playback_speed,
        clip_name=settings.clip_name or "Nieuwe clip",
        clip_duration_seconds=settings.clip_duration_seconds
        if settings.clip_duration_seconds is not None
        else source.clip_duration_seconds,
    )
    enqueue_download_for_player_song(clip, jobs=jobs)
    return clip
