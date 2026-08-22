"""Backfill legacy Player goal-song files into PlayerSong selections."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from django.db import migrations


_STORAGE_PREFIXES = ("goal_songs/", "player_songs/", "cached_songs/")


def _storage_name_from_uri(uri: object) -> str | None:
    """Return the existing storage key represented by a legacy media URL."""
    if not isinstance(uri, str) or not uri.strip():
        return None

    parsed = urlparse(uri.strip())
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None

    candidate = unquote(parsed.path or "").replace("\\", "/").lstrip("/")
    for prefix in _STORAGE_PREFIXES:
        marker = candidate.find(prefix)
        if marker < 0:
            continue
        name = PurePosixPath(candidate[marker:])
        if ".." in name.parts:
            return None
        return str(name)
    return None


def backfill_legacy_goal_songs(apps: Any, _schema_editor: Any) -> None:
    """Create one selected PlayerSong for every unmigrated legacy file."""
    Player = apps.get_model("player", "Player")
    PlayerSong = apps.get_model("player", "PlayerSong")

    players = Player.objects.exclude(goal_song_uri="").iterator(chunk_size=500)
    for player in players:
        if player.goal_song_song_ids:
            continue

        storage_name = _storage_name_from_uri(player.goal_song_uri)
        if storage_name is None:
            continue

        song = (
            PlayerSong.objects.filter(
                player_id=player.pk,
                audio_file=storage_name,
            )
            .order_by("created_at")
            .first()
        )
        if song is None:
            song = PlayerSong.objects.create(
                player_id=player.pk,
                cached_song_id=None,
                spotify_url="",
                title=PurePosixPath(storage_name).stem[:255] or "Legacy goal song",
                artists="",
                duration_seconds=None,
                start_time_seconds=max(0, int(player.song_start_time or 0)),
                playback_speed=1.0,
                status="ready",
                error_message="",
                audio_file=storage_name,
            )

        player.goal_song_song_ids = [str(song.id_uuid)]
        player.save(update_fields=["goal_song_song_ids"])


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0018_player_date_of_birth"),
    ]

    operations = [
        migrations.RunPython(
            backfill_legacy_goal_songs,
            migrations.RunPython.noop,
        ),
    ]
