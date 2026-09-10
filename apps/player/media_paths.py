"""Immutable, owner-scoped keys for user uploads."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4


if TYPE_CHECKING:
    from apps.player.models.player import Player
    from apps.player.models.player_song import PlayerSong


def player_picture_path(instance: Player, filename: str) -> str:
    """Isolate avatars even when different users supply identical filenames."""
    suffix = Path(filename).suffix.lower()
    return f"profile_pictures/{instance.pk}/{uuid4().hex}{suffix}"


def player_song_path(instance: PlayerSong, filename: str) -> str:
    """Never replace another upload or a cached clip through a name collision."""
    suffix = Path(filename).suffix.lower()
    return f"player_songs/{instance.player_id}/{uuid4().hex}{suffix}"


MEDIA_DOWNLOAD_SALT = "korfbal.media.download.v1"
