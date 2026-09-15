"""Overview and stats API views for player endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.services.player_overview import (
    build_player_overview_payload,
    build_player_stats_payload,
    connected_club_recent_results,
    player_seasons_queryset,
    resolve_season,
)
from apps.schedule.api.validation import uuid_query_values

from .common import (
    PLAYER_NOT_FOUND_DETAIL,
    PRIVATE_ACCOUNT_DETAIL,
    get_current_player,
    resolve_player_access,
)


class PlayerOverviewAPIView(KorfbalAPIView):
    """Expose player-specific match data grouped by season."""

    permission_classes = (permissions.AllowAny,)

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return upcoming and recent matches for the requested player."""
        access = resolve_player_access(
            request,
            player_id=player_id,
            visibility_field="stats_visibility",
        )
        player = access.player
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        if access.forbidden:
            return Response(
                PRIVATE_ACCOUNT_DETAIL,
                status=status.HTTP_403_FORBIDDEN,
            )

        seasons = list(player_seasons_queryset(player))
        season = resolve_season(request.query_params.get("season"), seasons)
        return Response(
            build_player_overview_payload(
                player=player,
                season=season,
                seasons=seasons,
            )
        )


class PlayerConnectedClubRecentResultsAPIView(KorfbalAPIView):
    """Return recent finished matches for the current player's followed clubs."""

    permission_classes = (permissions.AllowAny,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return match summaries for the player's followed clubs.

        Raises:
            ValidationError: The requested day window cannot be represented.

        """
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        try:
            limit = int(request.query_params.get("limit", "3"))
        except (TypeError, ValueError):
            limit = 3
        limit = max(1, min(limit, 10))

        days_param = request.query_params.get("days")
        days: int | None
        if not days_param:
            days = None
        else:
            try:
                days = int(days_param)
            except (TypeError, ValueError):
                days = None
            if days is not None and days <= 0:
                days = None

        season_id = request.query_params.get("season")
        if season_id:
            season_id = str(uuid_query_values([season_id], parameter="season")[0])
        if days is not None:
            try:
                timezone.now() - timedelta(days=days)
            except (OverflowError, ValueError):
                raise ValidationError({
                    "days": "Day window is outside the supported datetime range."
                }) from None

        return Response(
            connected_club_recent_results(
                player=player,
                limit=limit,
                days=days,
                season_id=season_id,
            )
        )


class PlayerStatsAPIView(KorfbalAPIView):
    """Expose season-scoped player shooting and scoring stats."""

    permission_classes = (permissions.AllowAny,)

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return aggregate stats for a player in a season."""
        access = resolve_player_access(
            request,
            player_id=player_id,
            visibility_field="stats_visibility",
        )
        player = access.player
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        if access.forbidden:
            return Response(
                PRIVATE_ACCOUNT_DETAIL,
                status=status.HTTP_403_FORBIDDEN,
            )

        seasons = list(player_seasons_queryset(player))
        season = resolve_season(request.query_params.get("season"), seasons)
        return Response(build_player_stats_payload(player=player, season=season))
