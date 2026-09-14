"""Team-level commands that coordinate player and fallback goal songs."""

from __future__ import annotations

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from apps.player.application.ports import SongDownloadDispatcher
from apps.player.models import PlayerSong
from apps.player.models.player import Player
from apps.player.services.player_song_queries import player_song_queryset
from apps.player.services.player_songs import (
    PlayerSongCreation,
    PlayerSongNotFoundError,
    PlayerSongSettingsPatch,
    create_owned_song,
    delete_owned_player_song,
    enqueue_download_for_player_song,
    retry_song_download,
)
from apps.team.models.team_data import TeamData


@transaction.atomic
def delete_team_player_song(
    *,
    player: Player,
    song_id: str,
    team_data: TeamData | None,
) -> None:
    """Delete an owned song and remove it from the team's fallback selection."""
    if team_data is not None:
        team_data = TeamData.objects.select_for_update().get(pk=team_data.pk)
    delete_owned_player_song(player=player, song_id=song_id)
    if team_data is None:
        return

    fallback_ids = [
        value for value in (team_data.fallback_goal_song_song_ids or []) if value
    ]
    next_ids = [value for value in fallback_ids if value != song_id]
    if next_ids == fallback_ids:
        return

    team_data.fallback_goal_song_song_ids = next_ids
    team_data.save(update_fields=["fallback_goal_song_song_ids"])


@transaction.atomic
def create_team_song(
    *,
    team_data: TeamData,
    uploaded_audio: UploadedFile | None,
    source_url: str,
    jobs: SongDownloadDispatcher,
) -> PlayerSongCreation:
    """Import into the team library and enable the song once its audio is ready."""
    owner = TeamData.objects.select_for_update().get(pk=team_data.pk)
    creation = create_owned_song(
        owner=owner,
        uploaded_audio=uploaded_audio,
        source_url=source_url,
        jobs=jobs,
    )
    ids = owner.fallback_goal_song_song_ids
    song_id = str(creation.song.pk)
    if song_id not in ids:
        owner.fallback_goal_song_song_ids = [*ids, song_id]
        owner.save(update_fields=["fallback_goal_song_song_ids"])
    return creation


def _locked_team_song(*, team_data: TeamData, song_id: str) -> PlayerSong:
    TeamData.objects.select_for_update().get(pk=team_data.pk)
    song = (
        player_song_queryset()
        .select_for_update(of=("self",))
        .filter(team_data=team_data, pk=song_id)
        .first()
    )
    if song is None:
        raise PlayerSongNotFoundError
    return song


@transaction.atomic
def update_team_song(
    *,
    team_data: TeamData,
    song_id: str,
    settings: PlayerSongSettingsPatch,
    jobs: SongDownloadDispatcher,
) -> PlayerSong:
    """Edit a team-owned clip without changing any player's personal settings."""
    song = _locked_team_song(team_data=team_data, song_id=song_id)
    if settings.start_time_seconds is not None:
        song.start_time_seconds = settings.start_time_seconds
    if settings.playback_speed is not None:
        song.playback_speed = settings.playback_speed
    song.save(update_fields=["start_time_seconds", "playback_speed", "updated_at"])
    if settings.start_time_seconds is not None:
        enqueue_download_for_player_song(song, jobs=jobs)
    return song


@transaction.atomic
def retry_team_song(
    *, team_data: TeamData, song_id: str, jobs: SongDownloadDispatcher
) -> PlayerSong:
    """Retry a failed import only within its owning team and season."""
    song = _locked_team_song(team_data=team_data, song_id=song_id)
    return retry_song_download(song=song, jobs=jobs)


@transaction.atomic
def delete_team_song(*, team_data: TeamData, song_id: str) -> None:
    """Delete team-owned audio and cascade its fallback selections."""
    _locked_team_song(team_data=team_data, song_id=song_id).delete()
