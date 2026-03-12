"""Upload/storage helpers for player media endpoints."""

from __future__ import annotations

from typing import cast

from django.core.files.uploadedfile import UploadedFile

from apps.player.models.player import Player
from apps.player.services.goal_song import (
    sanitize_uploaded_filename,
    store_goal_song_upload_best_effort,
)


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
    clip_duration_seconds: int,
) -> str:
    """Persist a goal-song upload and return its public URL."""
    filename = str(getattr(uploaded, "name", "goal_song") or "goal_song")
    safe_name = sanitize_uploaded_filename(filename, fallback="goal_song")

    _, url = store_goal_song_upload_best_effort(
        player=player,
        uploaded=uploaded,
        safe_name=safe_name,
        clip_duration_seconds=clip_duration_seconds,
    )

    player.goal_song_uri = url
    player.save(update_fields=["goal_song_uri"])
    return url
