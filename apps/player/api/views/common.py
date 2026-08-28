"""Shared helpers and constants for player API views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from django.conf import settings
from django.http import HttpResponseRedirect
from rest_framework.request import Request

from apps.player.models.player import Player
from apps.player.privacy import can_view_by_visibility
from apps.player.services.player_queries import (
    player_by_id,
    player_detail_queryset,
    player_for_user_id,
    viewer_player_for_user_id,
)


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
_VIEWER_CACHE_ATTRIBUTE: Final[str] = "_player_api_viewer"
_CURRENT_PLAYER_CACHE_ATTRIBUTE: Final[str] = "_player_api_current"
_CACHE_MISSING = object()
VisibilityField = Literal[
    "profile_picture_visibility",
    "stats_visibility",
    "teams_visibility",
]


@dataclass(frozen=True, slots=True)
class PlayerAccessResult:
    """Resolved player and visibility outcome for an API request."""

    player: Player | None
    forbidden: bool = False


def _authenticated_user_id(request: Request) -> int | None:
    if not getattr(request.user, "is_authenticated", False):
        return None
    user_id = getattr(request.user, "id", None)
    return user_id if isinstance(user_id, int) else None


def get_current_player(request: Request) -> Player | None:
    """Resolve the current player from the request context."""
    user_id = _authenticated_user_id(request)
    if user_id is not None:
        cached = getattr(request, _CURRENT_PLAYER_CACHE_ATTRIBUTE, _CACHE_MISSING)
        if cached is not _CACHE_MISSING:
            return cached if isinstance(cached, Player) else None

        player = player_for_user_id(user_id)
        setattr(request, _CURRENT_PLAYER_CACHE_ATTRIBUTE, player)
        setattr(request, _VIEWER_CACHE_ATTRIBUTE, player)
        return player

    if settings.DEBUG:
        player_id = request.query_params.get("player_id")
        if player_id:
            player = player_by_id(player_id)
            if player:
                return player
        return player_detail_queryset().first()

    return None


def get_viewer_player(request: Request) -> Player | None:
    """Resolve the viewer player when the request is authenticated."""
    user_id = _authenticated_user_id(request)
    if user_id is None:
        return None

    cached = getattr(request, _VIEWER_CACHE_ATTRIBUTE, _CACHE_MISSING)
    if cached is not _CACHE_MISSING:
        return cached if isinstance(cached, Player) else None

    player = viewer_player_for_user_id(user_id)
    setattr(request, _VIEWER_CACHE_ATTRIBUTE, player)
    return player


def cache_viewer_player(request: Request, player: Player) -> None:
    """Cache an already-loaded player as the authenticated request viewer."""
    if player.user_id == _authenticated_user_id(request):
        setattr(request, _VIEWER_CACHE_ATTRIBUTE, player)


def player_serializer_context(
    request: Request,
    *,
    current_player: Player | None = None,
) -> dict[str, object]:
    """Build query-free PlayerSerializer context for one request."""
    viewer = current_player
    user_id = _authenticated_user_id(request)
    if viewer is None or viewer.user_id != user_id:
        viewer = get_viewer_player(request)
    return {"request": request, "viewer_player": viewer}


def resolve_player_access(
    request: Request,
    *,
    player_id: str | None,
    visibility_field: VisibilityField | None = None,
) -> PlayerAccessResult:
    """Resolve a current or explicit player and evaluate optional visibility."""
    player = player_by_id(player_id) if player_id else get_current_player(request)
    if player is None:
        return PlayerAccessResult(player=None)
    if not player_id or visibility_field is None:
        return PlayerAccessResult(player=player)

    visibility = str(getattr(player, visibility_field))
    if player.user_id == _authenticated_user_id(request):
        return PlayerAccessResult(player=player)
    if visibility == Player.Visibility.PUBLIC:
        return PlayerAccessResult(player=player)

    allowed = can_view_by_visibility(
        visibility=visibility,
        viewer=get_viewer_player(request),
        target=player,
    )
    return PlayerAccessResult(player=player, forbidden=not allowed)


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
