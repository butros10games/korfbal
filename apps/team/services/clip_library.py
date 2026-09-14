"""Search reusable team clips and copy them into an independently owned library."""

from io import BytesIO
from pathlib import Path

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet

from apps.player.application.ports import AudioStorage, SongDownloadDispatcher
from apps.player.media_paths import player_song_path
from apps.player.models import PlayerSong, PlayerSongStatus
from apps.player.services.player_song_queries import player_song_queryset
from apps.player.services.player_songs import (
    InvalidSongClipError,
    PlayerSongCreation,
    PlayerSongNotFoundError,
    PlayerSongSettingsPatch,
    enqueue_download_for_player_song,
    validate_song_clip_settings,
)
from apps.player.services.upload_validation import (
    MAX_AUDIO_UPLOAD_BYTES,
    validate_audio_upload,
)
from apps.team.models import TeamData


def ready_team_clips() -> QuerySet[PlayerSong]:
    """Keep personal libraries and unavailable sources out of shared discovery."""
    return (
        player_song_queryset()
        .filter(team_data__isnull=False)
        .filter(
            Q(
                cached_song__isnull=False,
                cached_song__status=PlayerSongStatus.READY,
                cached_song__audio_file__isnull=False,
            )
            & ~Q(cached_song__audio_file="")
            | Q(
                cached_song__isnull=True,
                status=PlayerSongStatus.READY,
                audio_file__isnull=False,
            )
            & ~Q(audio_file="")
        )
    )


def search_team_clips(*, owner: TeamData, search: str) -> QuerySet[PlayerSong]:
    """Search in SQL with deterministic pagination and recipient-specific receipts."""
    queryset = (
        ready_team_clips()
        .exclude(team_data=owner)
        .select_related("team_data__team__club", "team_data__season")
        .annotate(
            library_added=Exists(
                PlayerSong.objects.filter(
                    team_data=owner, library_source_id=OuterRef("pk")
                )
            )
        )
    )
    for term in search.split():
        queryset = queryset.filter(
            Q(title__icontains=term)
            | Q(artists__icontains=term)
            | Q(cached_song__title__icontains=term)
            | Q(cached_song__artists__icontains=term)
            | Q(clip_name__icontains=term)
            | Q(team_data__team__name__icontains=term)
            | Q(team_data__team__club__name__icontains=term)
        )
    return queryset.order_by("-created_at", "pk")


@transaction.atomic
def add_team_library_clip(
    *,
    owner: TeamData,
    source_id: str,
    storage: AudioStorage,
    jobs: SongDownloadDispatcher,
) -> PlayerSongCreation:
    """Copy settings and audio ownership once, then select the recipient's clip.

    Raises:
        PlayerSongNotFoundError: The source is not a ready clip from another team.
        InvalidSongClipError: Stored clip settings or audio are unavailable.

    """
    owner = TeamData.objects.select_for_update().get(pk=owner.pk)
    source = (
        ready_team_clips()
        .exclude(team_data=owner)
        .select_for_update(of=("self",))
        .filter(pk=source_id)
        .first()
    )
    if source is None:
        raise PlayerSongNotFoundError
    existing = (
        player_song_queryset().filter(team_data=owner, library_source=source).first()
    )
    if existing is not None:
        song = existing
    else:
        validate_song_clip_settings(source, PlayerSongSettingsPatch())
        song = PlayerSong(
            team_data=owner,
            library_source=source,
            cached_song=source.cached_song,
            spotify_url=source.spotify_url,
            title=source.title,
            artists=source.artists,
            duration_seconds=source.duration_seconds,
            status=PlayerSongStatus.READY,
            clip_name=source.clip_name,
            start_time_seconds=source.start_time_seconds,
            clip_duration_seconds=source.clip_duration_seconds,
            playback_speed=source.playback_speed,
        )
        if source.cached_song_id is None:
            audio_key = source.audio_file.name
            if not audio_key:
                raise PlayerSongNotFoundError
            try:
                with storage.open(audio_key) as stream:
                    content = stream.read(MAX_AUDIO_UPLOAD_BYTES + 1)
            except OSError as exc:
                raise InvalidSongClipError(
                    "De audio is niet beschikbaar. Probeer later opnieuw."
                ) from exc
            upload = UploadedFile(
                file=BytesIO(content),
                name=Path(audio_key).name,
                content_type="audio/mpeg",
                size=len(content),
            )
            validate_audio_upload(upload)
            song.audio_file = storage.save_bytes(
                player_song_path(song, upload.name or "clip.mp3"), content
            )
        song.save()
        enqueue_download_for_player_song(song, jobs=jobs)
    ids = owner.fallback_goal_song_song_ids
    if str(song.pk) not in ids:
        owner.fallback_goal_song_song_ids = [*ids, str(song.pk)]
        owner.save(update_fields=["fallback_goal_song_song_ids"])
    return PlayerSongCreation(song=song, created=existing is None)
