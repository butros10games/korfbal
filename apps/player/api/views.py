"""Views powering the player API endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, ClassVar

from django.conf import settings
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.game_tracker.models import MatchData, Shot
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.player.models.player import Player
from apps.schedule.models import Season

from .serializers import PlayerSerializer


PLAYER_NOT_FOUND_DETAIL = {"detail": "Player not found"}


def _get_current_player(request: Request) -> Player | None:
    """Resolve the current player from the request context.

    Args:
        request (Request): Incoming request.

    Returns:
        Player | None: The resolved player or ``None`` when not found.

    """
    queryset = Player.objects.select_related("user").prefetch_related(
        "team_follow",
        "club_follow",
    )

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


class PlayerViewSet(viewsets.ModelViewSet):
    """Provide CRUD operations for players."""

    queryset = Player.objects.select_related("user").prefetch_related(
        "team_follow",
        "club_follow",
    )
    serializer_class = PlayerSerializer
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticatedOrReadOnly,
    ]
    lookup_field = "id_uuid"


class CurrentPlayerAPIView(APIView):
    """Return the profile for the active player (or a debug fallback)."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the current player's profile.

        Args:
            request (Request): The request object.
            *args (Any): Positional arguments.
            **kwargs (Any): Keyword arguments.

        Returns:
            Response: The serialized player profile.

        """
        player = _get_current_player(request)

        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerSerializer(player)
        return Response(serializer.data)


class PlayerOverviewAPIView(APIView):
    """Expose player-specific match data grouped by season."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return upcoming and recent matches for the requested player.

        Supports both the authenticated player (``/me/overview/``) and
        explicit player lookups (``/players/<uuid>/overview/``).
        """
        player = self._resolve_player(request, player_id)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        seasons_qs = list(self._player_seasons_queryset(player))
        season = self._resolve_season(request, seasons_qs)

        upcoming_matches = build_match_summaries(
            self._match_queryset_for_player(player, season, include_roster=True)
            .filter(status__in=["upcoming", "active"])
            .order_by("match_link__start_time")[:10]
        )

        recent_matches = build_match_summaries(
            self._match_queryset_for_player(player, season, include_roster=False)
            .filter(status="finished")
            .order_by("-match_link__start_time")[:10]
        )

        current_season = self._current_season()
        seasons_payload = [
            {
                "id_uuid": str(option.id_uuid),
                "name": option.name,
                "start_date": option.start_date.isoformat(),
                "end_date": option.end_date.isoformat(),
                "is_current": current_season is not None
                and option.id_uuid == current_season.id_uuid,
            }
            for option in seasons_qs
        ]

        payload = {
            "matches": {
                "upcoming": upcoming_matches,
                "recent": recent_matches,
            },
            "seasons": seasons_payload,
            "meta": {
                "season_id": str(season.id_uuid) if season else None,
                "season_name": season.name if season else None,
            },
        }

        return Response(payload)

    @staticmethod
    def _resolve_player(request: Request, player_id: str | None) -> Player | None:
        if player_id:
            return (
                Player.objects.select_related("user")
                .prefetch_related("team_follow", "club_follow")
                .filter(id_uuid=player_id)
                .first()
            )

        return _get_current_player(request)

    @staticmethod
    def _match_queryset_for_player(
        player: Player,
        season: Season | None,
        *,
        include_roster: bool,
    ) -> QuerySet[MatchData]:
        participation_filter = Q(player_groups__players=player)
        roster_filter = Q()
        if include_roster:
            roster_filter = Q(
                Q(match_link__home_team__team_data__players=player)
                | Q(match_link__away_team__team_data__players=player)
            )

        queryset = MatchData.objects.select_related(
            "match_link",
            "match_link__home_team",
            "match_link__home_team__club",
            "match_link__away_team",
            "match_link__away_team__club",
            "match_link__season",
        ).prefetch_related(
            "player_groups__players",
        )

        queryset = queryset.filter(participation_filter | roster_filter)

        if season:
            queryset = queryset.filter(match_link__season=season)

        return queryset.distinct()

    @staticmethod
    def _player_seasons_queryset(player: Player) -> QuerySet[Season]:
        return (
            Season.objects.filter(
                Q(team_data__players=player)
                | Q(matches__matchdata__player_groups__players=player)
                | Q(matches__matchdata__shots__player=player)
                | Q(matches__home_team__team_data__players=player)
                | Q(matches__away_team__team_data__players=player)
            )
            .distinct()
            .order_by("-start_date")
        )

    def _resolve_season(self, request: Request, seasons: list[Season]) -> Season | None:
        season_param = request.query_params.get("season")
        if season_param:
            return next(
                (option for option in seasons if str(option.id_uuid) == season_param),
                None,
            )

        if not seasons:
            return None

        current = self._current_season()
        if current and any(option.id_uuid == current.id_uuid for option in seasons):
            return current

        return seasons[0]

    def _current_season(self) -> Season | None:
        today = timezone.now().date()
        return Season.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
        ).first()


class PlayerConnectedClubRecentResultsAPIView(APIView):
    """Return recent finished matches for the current player's followed clubs.

    This endpoint exists because the player overview endpoint is scoped to matches
    where the player participated (or is on the roster). For the Home 'Updates'
    widget we want the latest results for the player's connected clubs.

    Query params:
        - limit: int (default 3, max 10)
        - days: int (optional; when provided, only matches within the last N days)
        - season: UUID (optional; restrict to a season)
        - player_id: UUID (debug-only; supported via _get_current_player)
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return match summaries for the player's followed clubs."""
        player = _get_current_player(request)
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

        clubs_qs = player.club_follow.all()
        if not clubs_qs.exists():
            return Response([])

        queryset = (
            MatchData.objects.select_related(
                "match_link",
                "match_link__home_team",
                "match_link__home_team__club",
                "match_link__away_team",
                "match_link__away_team__club",
                "match_link__season",
            )
            .filter(
                status="finished",
            )
            .filter(
                Q(match_link__home_team__club__in=clubs_qs)
                | Q(match_link__away_team__club__in=clubs_qs)
            )
            .distinct()
        )

        if season_id:
            queryset = queryset.filter(match_link__season_id=season_id)

        if days is not None:
            cutoff = timezone.now() - timedelta(days=days)
            queryset = queryset.filter(match_link__start_time__gte=cutoff)

        matches = build_match_summaries(
            queryset.order_by("-match_link__start_time")[:limit]
        )
        return Response(matches)


class PlayerStatsAPIView(APIView):
    """Expose season-scoped player shooting and scoring stats."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return aggregate stats for a player in a season."""
        player = self._resolve_player(request, player_id)
        if player is None:
            return Response(
                {"detail": "Player not found"}, status=status.HTTP_404_NOT_FOUND
            )

        seasons_qs = list(self._player_seasons_queryset(player))
        season = self._resolve_season(request, seasons_qs)

        shot_queryset = Shot.objects.select_related("match_data", "shot_type").filter(
            player=player
        )
        if season:
            shot_queryset = shot_queryset.filter(match_data__match_link__season=season)

        aggregated = shot_queryset.aggregate(
            shots_for=Count("id_uuid", filter=Q(for_team=True)),
            shots_against=Count("id_uuid", filter=Q(for_team=False)),
            goals_for=Count("id_uuid", filter=Q(for_team=True, scored=True)),
            goals_against=Count("id_uuid", filter=Q(for_team=False, scored=True)),
        )

        goal_types_for = self._goal_type_breakdown(shot_queryset, for_team=True)
        goal_types_against = self._goal_type_breakdown(shot_queryset, for_team=False)

        payload = {
            "shots_for": int(aggregated.get("shots_for", 0)),
            "shots_against": int(aggregated.get("shots_against", 0)),
            "goals_for": int(aggregated.get("goals_for", 0)),
            "goals_against": int(aggregated.get("goals_against", 0)),
            "goal_types": {
                "for": goal_types_for,
                "against": goal_types_against,
            },
        }

        return Response(payload)

    @staticmethod
    def _resolve_player(request: Request, player_id: str | None) -> Player | None:
        if player_id:
            return (
                Player.objects.select_related("user").filter(id_uuid=player_id).first()
            )

        return _get_current_player(request)

    def _resolve_season(self, request: Request, seasons: list[Season]) -> Season | None:
        season_param = request.query_params.get("season")
        if season_param:
            return next(
                (option for option in seasons if str(option.id_uuid) == season_param),
                None,
            )

        if not seasons:
            return None

        current = self._current_season()
        if current and any(option.id_uuid == current.id_uuid for option in seasons):
            return current

        return seasons[0]

    @staticmethod
    def _player_seasons_queryset(player: Player) -> QuerySet[Season]:
        return (
            Season.objects.filter(
                Q(team_data__players=player)
                | Q(matches__matchdata__player_groups__players=player)
                | Q(matches__matchdata__shots__player=player)
                | Q(matches__home_team__team_data__players=player)
                | Q(matches__away_team__team_data__players=player)
            )
            .distinct()
            .order_by("-start_date")
        )

    def _current_season(self) -> Season | None:
        today = timezone.now().date()
        return Season.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
        ).first()

    @staticmethod
    def _goal_type_breakdown(
        queryset: QuerySet[Shot], *, for_team: bool
    ) -> list[dict[str, str | int | None]]:
        breakdown = (
            queryset.filter(for_team=for_team, scored=True)
            .values("shot_type__id_uuid", "shot_type__name")
            .annotate(count=Count("id_uuid"))
            .order_by("shot_type__name")
        )

        return [
            {
                "id_uuid": str(row.get("shot_type__id_uuid"))
                if row.get("shot_type__id_uuid")
                else None,
                "name": row.get("shot_type__name") or "Onbekend",
                "count": int(row.get("count", 0)),
            }
            for row in breakdown
        ]
