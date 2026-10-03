"""Immutable, owner-scoped keys for user uploads."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4


if TYPE_CHECKING:
    from django.core.files.storage import Storage

    from apps.player.models.player import Player
    from apps.player.models.player_song import PlayerSong


def player_picture_path(instance: Player, filename: str) -> str:
    """Isolate avatars even when different users supply identical filenames."""
    suffix = Path(filename).suffix.lower()
    return f"{PROFILE_PICTURE_PREFIX}{instance.pk}/{uuid4().hex}{suffix}"


def player_song_path(instance: PlayerSong, filename: str) -> str:
    """Never replace another upload or a cached clip through a name collision."""
    suffix = Path(filename).suffix.lower()
    team_data_id = getattr(instance, "team_data_id", None)
    if team_data_id is not None:
        return f"team_songs/{team_data_id}/{uuid4().hex}{suffix}"
    return f"player_songs/{instance.player_id}/{uuid4().hex}{suffix}"


MEDIA_DOWNLOAD_SALT = "korfbal.media.download.v1"
PROFILE_PICTURE_PREFIX = "profile_pictures/"
# Resized copies of logos and pictures; see apps.player.services.image_variants.
VARIANT_SIZE = 256
VARIANT = f"w{VARIANT_SIZE}"


def variant_key(key: str) -> str:
    """Return where the resized copy of a stored image lives, beside its original."""
    return f"{key}.{VARIANT}.webp"


def delete_with_variant(storage: Storage, key: str) -> None:
    """Erase a stored picture together with its resized copy.

    The original goes first: a copy that a concurrent request writes afterwards
    is removed by that request once it sees the original is gone.
    """
    storage.delete(key)
    storage.delete(variant_key(key))


def variant_url(url: str) -> str:
    """Ask the media download endpoint for the resized copy of a picture."""
    parts = urlsplit(url)
    query = urlencode([*parse_qsl(parts.query), ("variant", VARIANT)])
    return parts._replace(query=query).geturl()
