"""Query services for player-song read models and ownership checks."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import models
from django.db.models import QuerySet

from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong


def player_song_queryset() -> QuerySet[PlayerSong]:
    """Return songs with the effective audio source loaded."""
    return PlayerSong.objects.select_related("cached_song").fetch_mode(
        models.FETCH_RAISE
    )


def player_song_by_id(song_id: str) -> PlayerSong | None:
    """Return one API-ready song by public id."""
    return player_song_queryset().filter(id_uuid=song_id).first()


def player_songs_for_player(player: Player) -> QuerySet[PlayerSong]:
    """Return a player's songs in API order."""
    return player_song_queryset().filter(player=player).order_by("-created_at")


def player_songs_for_players(players: Iterable[Player]) -> QuerySet[PlayerSong]:
    """Return songs for multiple players in API order."""
    return player_song_queryset().filter(player__in=players).order_by("-created_at")


def player_songs_by_ids(
    *,
    song_ids: Iterable[str],
    player: Player | None = None,
) -> QuerySet[PlayerSong]:
    """Return API-ready songs for a bounded set of ids and optional owner."""
    queryset = player_song_queryset().filter(id_uuid__in=song_ids)
    if player is not None:
        queryset = queryset.filter(player=player)
    return queryset


def owned_player_song_or_none(
    *,
    player: Player,
    song_id: str,
    for_update: bool = False,
) -> PlayerSong | None:
    """Return an owned song, optionally locking it for a state transition."""
    queryset = player_song_queryset().filter(player=player, id_uuid=song_id)
    if for_update:
        # `cached_song` is nullable, so lock only the PlayerSong side of the
        # outer join. Commands that mutate shared cache state lock it explicitly.
        queryset = queryset.select_for_update(of=("self",))
    return queryset.first()
