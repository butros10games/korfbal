"""Upload/storage helpers for player media endpoints."""

from __future__ import annotations

from typing import cast

from django.core.files.uploadedfile import UploadedFile

from apps.player.application.ports import SongDownloadDispatcher
from apps.player.models.player import Player
from apps.player.services.goal_song import (
    apply_goal_song_song_ids,
    sanitize_uploaded_filename,
)
from apps.player.services.player_songs import create_player_song


ALLOWED_GOAL_SONG_CONTENT_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "audio/mp4",
    "audio/x-m4a",
}


def uploaded_file_or_none(value: object) -> UploadedFile | None:
    """Return a typed uploaded file when the payload looks valid."""
    if isinstance(value, UploadedFile) or hasattr(value, "name"):
        return cast(UploadedFile, value)
    return None


def goal_song_content_type_allowed(uploaded: UploadedFile) -> bool:
    """Return whether the uploaded goal-song content type is acceptable."""
    content_type = (getattr(uploaded, "content_type", "") or "").lower()
    return not content_type or content_type in ALLOWED_GOAL_SONG_CONTENT_TYPES


def save_profile_picture_upload(*, player: Player, uploaded: UploadedFile) -> str:
    """Persist a profile picture upload and return its public URL."""
    filename = getattr(uploaded, "name", "profile_picture")
    player.profile_picture.save(filename, uploaded)
    return player.get_profile_picture()


def save_goal_song_upload(
    *,
    player: Player,
    uploaded: UploadedFile,
    jobs: SongDownloadDispatcher,
) -> str:
    """Create and select a PlayerSong from the legacy upload endpoint."""
    filename = str(getattr(uploaded, "name", "goal_song") or "goal_song")
    safe_name = sanitize_uploaded_filename(filename, fallback="goal_song")
    uploaded.name = safe_name

    song, _created = create_player_song(
        player=player,
        uploaded_audio=uploaded,
        spotify_url=None,
        jobs=jobs,
    )
    update_fields = apply_goal_song_song_ids(
        player=player,
        ids=[str(song.id_uuid)],
    )
    player.save(update_fields=update_fields)

    return str(song.audio_file.url)
