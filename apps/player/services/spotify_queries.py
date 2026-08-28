"""Query services for persisted Spotify connections."""

from __future__ import annotations

from django.db import models
from django.db.models import QuerySet

from apps.player.models.spotify_token import SpotifyToken


def spotify_token_queryset() -> QuerySet[SpotifyToken]:
    """Return Spotify tokens with accidental relation fetches disabled."""
    return SpotifyToken.objects.all().fetch_mode(models.FETCH_RAISE)


def spotify_token_for_user_id(
    user_id: int,
    *,
    for_update: bool = False,
) -> SpotifyToken | None:
    """Return a user's token, optionally locking it for refresh."""
    queryset = spotify_token_queryset().filter(user_id=user_id)
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    return queryset.first()
