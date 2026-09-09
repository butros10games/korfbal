"""Upload/storage helpers for player media endpoints."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import cast
import warnings

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.player.application.ports import SongDownloadDispatcher
from apps.player.models.player import Player
from apps.player.services.goal_song import (
    apply_goal_song_song_ids,
    sanitize_uploaded_filename,
)
from apps.player.services.player_songs import create_player_song


MAX_AVATAR_DIMENSION = 4096


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


class InvalidProfilePictureError(ValueError):
    """The upload is not a bounded, decodable image."""


def save_profile_picture_upload(*, player: Player, uploaded: UploadedFile) -> str:
    """Validate and re-encode an avatar, discarding metadata and active content.

    Raises:
        InvalidProfilePictureError: The file exceeds limits or is not an image.

    """
    if uploaded.size is None or uploaded.size > 5 * 1024 * 1024:
        raise InvalidProfilePictureError("Profile pictures must be at most 5 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(uploaded) as original:
                if (
                    original.width > MAX_AVATAR_DIMENSION
                    or original.height > MAX_AVATAR_DIMENSION
                ):
                    raise InvalidProfilePictureError(
                        "Profile pictures must be at most 4096 pixels per side."
                    )
                original.load()
                decoded = ImageOps.exif_transpose(original).convert("RGBA")
                decoded.thumbnail((1024, 1024))
                # A fresh image intentionally drops EXIF, text chunks, and profiles.
                clean = Image.new("RGBA", decoded.size)
                clean.paste(decoded)
                output = BytesIO()
                clean.save(output, format="PNG")
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise InvalidProfilePictureError(
            "Upload a valid image no larger than 4096 pixels per side."
        ) from error
    stem = sanitize_uploaded_filename(
        Path(uploaded.name or "avatar").stem, fallback="avatar"
    )
    player.profile_picture.save(f"{stem}.png", ContentFile(output.getvalue()))
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

    creation = create_player_song(
        player=player,
        uploaded_audio=uploaded,
        spotify_url=None,
        jobs=jobs,
    )
    song = creation.song
    update_fields = apply_goal_song_song_ids(
        player=player,
        ids=[str(song.id_uuid)],
    )
    player.save(update_fields=update_fields)

    return str(song.audio_file.url)
