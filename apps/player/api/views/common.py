"""Shared helpers and constants for player API views."""

from __future__ import annotations

import logging
from typing import Final

from django.conf import settings
from django.db.models import QuerySet
from django.http import HttpResponseRedirect
from rest_framework.request import Request

from apps.player.models.player import Player


logger = logging.getLogger(__name__)

TEST_PUSH_ERROR_LIMIT: Final[int] = 10

PLAYER_NOT_FOUND_MESSAGE = "Player not found"
PLAYER_NOT_FOUND_DETAIL = {"detail": PLAYER_NOT_FOUND_MESSAGE}
SONG_NOT_FOUND_DETAIL = {"detail": "Song not found"}

AUTHENTICATION_REQUIRED_MESSAGE = "Authentication required"
AUTHENTICATION_REQUIRED_DETAIL = {"detail": AUTHENTICATION_REQUIRED_MESSAGE}

PRIVATE_ACCOUNT_MESSAGE = "Private account"
PRIVATE_ACCOUNT_DETAIL = {"code": "private_account", "detail": PRIVATE_ACCOUNT_MESSAGE}

SPOTIFY_NOT_CONFIGURED_MESSAGE = "Spotify is not configured on the server"
SPOTIFY_NOT_CONFIGURED_DETAIL = {"detail": SPOTIFY_NOT_CONFIGURED_MESSAGE}


def player_detail_queryset() -> QuerySet[Player]:
    """Return the default queryset used by player API endpoints."""
    return Player.objects.select_related("user").prefetch_related(
        "team_follow",
        "club_follow",
        "member_clubs",
        "club_membership_links",
        "club_membership_links__club",
    )


def get_current_player(request: Request) -> Player | None:
    """Resolve the current player from the request context."""
    queryset = player_detail_queryset()

    if request.user.is_authenticated:
        try:
            return queryset.get(user=request.user)
        except Player.DoesNotExist:
            return None

    if settings.DEBUG:
        player_id = request.query_params.get("player_id")
        if player_id:
            player = queryset.filter(id_uuid=player_id).first()
            if player:
                return player
        return queryset.first()

    return None


def get_viewer_player(request: Request) -> Player | None:
    """Resolve the viewer player when the request is authenticated."""
    if not request.user.is_authenticated:
        return None
    return Player.objects.filter(user=request.user).first()


def redirect_to_frontend(
    redirect_path: str | None = None,
) -> HttpResponseRedirect:
    """Redirect the user back to the SPA frontend."""
    web_origin = getattr(settings, "WEB_APP_ORIGIN", "").rstrip("/")
    if not web_origin:
        return HttpResponseRedirect("/")

    if isinstance(redirect_path, str) and redirect_path.startswith("/"):
        return HttpResponseRedirect(f"{web_origin}{redirect_path}")

    return HttpResponseRedirect(f"{web_origin}/")
