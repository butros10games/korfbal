"""Views for schedule endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db.models import Q, QuerySet
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.live_update_signal_control import (
    suppress_tracker_delete_side_effects,
)
from apps.kwt_common.api.pagination import ScheduleEditorPagination
from apps.kwt_common.api.permissions import IsStaffOrReadOnly
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.player.models.player import Player
from apps.schedule.models import Match

from .match_viewset_events import MatchEventsActionsMixin
from .match_viewset_live import MatchLiveActionsMixin
from .match_viewset_mvp import MatchMvpActionsMixin
from .match_viewset_stats import MatchStatsActionsMixin
from .serializers import MatchSerializer, MatchWriteSerializer
from .tracker_access import TrackerAccessActionsMixin
from .validation import UUID_URL_REGEX, uuid_query_values


def _uuid_path_parameter(name: str) -> OpenApiParameter:
    return OpenApiParameter(name, OpenApiTypes.UUID, OpenApiParameter.PATH)


@extend_schema_view(
    retrieve=extend_schema(parameters=[_uuid_path_parameter("id")]),
    update=extend_schema(parameters=[_uuid_path_parameter("id")]),
    partial_update=extend_schema(parameters=[_uuid_path_parameter("id")]),
    destroy=extend_schema(parameters=[_uuid_path_parameter("id")]),
    resolve_event_reconciliation=extend_schema(
        parameters=[_uuid_path_parameter("reconciliation_id")]
    ),
    goal_detail=extend_schema(parameters=[_uuid_path_parameter("shot_id")]),
    possession_change_detail=extend_schema(
        parameters=[
            OpenApiParameter(
                "event_id",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
            )
        ]
    ),
    substitute_detail=extend_schema(parameters=[_uuid_path_parameter("change_id")]),
    pause_detail=extend_schema(parameters=[_uuid_path_parameter("pause_id")]),
    timeout_detail=extend_schema(parameters=[_uuid_path_parameter("timeout_id")]),
    tracker_access=extend_schema(parameters=[_uuid_path_parameter("team_id")]),
    tracker_access_redeem=extend_schema(parameters=[_uuid_path_parameter("team_id")]),
    tracker_state=extend_schema(parameters=[_uuid_path_parameter("team_id")]),
    tracker_command=extend_schema(parameters=[_uuid_path_parameter("team_id")]),
    tracker_poll=extend_schema(parameters=[_uuid_path_parameter("team_id")]),
)
class MatchViewSet(
    MatchMvpActionsMixin,
    MatchStatsActionsMixin,
    MatchLiveActionsMixin,
    TrackerAccessActionsMixin,
    MatchEventsActionsMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """Expose match data for the mobile frontend."""

    pagination_class = ScheduleEditorPagination
    serializer_class = MatchSerializer
    permission_classes = (IsStaffOrReadOnly,)
    lookup_field = "id_uuid"
    lookup_url_kwarg = "id"
    lookup_value_regex = UUID_URL_REGEX

    def get_serializer_class(self) -> type[MatchSerializer | MatchWriteSerializer]:
        """Use an explicit flat serializer for staff schedule writes."""
        if self.action in {"create", "update", "partial_update"}:
            return MatchWriteSerializer
        return MatchSerializer

    def perform_destroy(self, instance: Match) -> None:
        """Delete a match without recreating tracker rows during its cascade."""
        with suppress_tracker_delete_side_effects():
            instance.delete()

    @staticmethod
    def _match_data(match: Match) -> MatchData | None:
        """Return eagerly loaded tracker data without issuing a fallback query."""
        try:
            return match.tracker_data
        except MatchData.DoesNotExist:
            return None

    def get_queryset(self) -> QuerySet[Match]:
        """Return a queryset filtered by the current request context.

        Returns:
            QuerySet[Match]: Filtered match queryset.

        """
        queryset = (
            Match.objects
            .select_related(
                "home_team__club",
                "away_team__club",
                "season",
                "pool",
                "tracker_data",
            )
            .order_by("start_time", "id_uuid")
            .fetch_mode(models.FETCH_RAISE)
        )

        team_ids = uuid_query_values(
            self.request.query_params.getlist("team"), parameter="team"
        )
        club_ids = uuid_query_values(
            self.request.query_params.getlist("club"), parameter="club"
        )
        season_ids = uuid_query_values(
            self.request.query_params.getlist("season"), parameter="season"
        )

        if not team_ids and self.request.query_params.get("followed", "").lower() in {
            "true",
            "1",
        }:
            player = self._get_player()
            if player:
                team_ids = list(player.team_follow.values_list("id_uuid", flat=True))
            queryset = queryset.filter(
                Q(home_team__id_uuid__in=team_ids) | Q(away_team__id_uuid__in=team_ids)
            )

        elif team_ids:
            queryset = queryset.filter(
                Q(home_team__id_uuid__in=team_ids) | Q(away_team__id_uuid__in=team_ids)
            )

        if club_ids:
            queryset = queryset.filter(
                Q(home_team__club__id_uuid__in=club_ids)
                | Q(away_team__club__id_uuid__in=club_ids)
            )

        if season_ids:
            queryset = queryset.filter(season__id_uuid=season_ids[-1])

        if self.action == "list":
            pool = self.request.query_params.get("pool")
            if pool == "unassigned":
                queryset = queryset.filter(pool__isnull=True)
            elif pool:
                pool_ids = uuid_query_values([pool], parameter="pool")
                queryset = queryset.filter(pool_id=pool_ids[-1])
            search = self.request.query_params.get("search", "").strip()
            if search:
                queryset = queryset.filter(
                    Q(home_team__name__icontains=search)
                    | Q(home_team__club__name__icontains=search)
                    | Q(away_team__name__icontains=search)
                    | Q(away_team__club__name__icontains=search)
                    | Q(pool__name__icontains=search)
                )
        return queryset

    def _get_player(self) -> Player | None:
        """Return the authenticated player (or debug override).

        Returns:
            Player | None: The player instance or None.

        """
        if self.request.user.is_authenticated:
            try:
                return Player.objects.prefetch_related("team_follow").get(
                    user=self.request.user
                )
            except Player.DoesNotExist:
                return None

        if settings.DEBUG:
            player_id = self.request.query_params.get("player_id")
            if player_id:
                return (
                    Player.objects
                    .prefetch_related("team_follow")
                    .filter(
                        id_uuid=player_id,
                    )
                    .first()
                )
        return None

    def _upcoming_queryset(self) -> QuerySet[Match]:
        """Return upcoming matches ordered by start time.

        Returns:
            QuerySet[Match]: Upcoming matches.

        """
        now = timezone.now()
        return self.get_queryset().filter(start_time__gte=now).order_by("start_time")

    def _is_cacheable_public_request(self) -> bool:
        """Return True when it is safe to cache a response for this request.

        We deliberately skip caching for authenticated callers and for requests
        that use the user-specific `followed` filter.
        """
        if self.request.user.is_authenticated:
            return False
        return not self.request.query_params.get("followed")

    def _public_cache_key(self) -> str:
        """Cache key that varies by full path (including query string)."""
        return f"korfbal:schedule:{self.request.get_full_path()}"

    @action(detail=False, methods=("GET",), url_path="next")
    def next_match(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return the next upcoming match for the active context.

        Returns:
            Response: Serialized next match.

        """
        if self._is_cacheable_public_request():
            cache_key = self._public_cache_key()
            cache_miss = object()
            cached_payload = cache.get(cache_key, cache_miss)
            if cached_payload is not cache_miss:
                return Response(cached_payload)

        match = self._upcoming_queryset().first()
        if not match:
            payload: Any = None
            if self._is_cacheable_public_request():
                cache.set(self._public_cache_key(), payload, timeout=30)
            return Response(payload, status=status.HTTP_200_OK)
        serializer = self.get_serializer(match)
        payload = serializer.data
        if self._is_cacheable_public_request():
            cache.set(self._public_cache_key(), payload, timeout=30)
        return Response(payload)

    @action(detail=False, methods=("GET",), url_path="upcoming")
    def upcoming(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return a limited list of upcoming matches.

        Returns:
            Response: Serialized list of upcoming matches.

        """
        limit_param = request.query_params.get("limit")
        try:
            limit = int(limit_param) if limit_param else 5
        except ValueError:
            limit = 5

        queryset = self._upcoming_queryset()[: max(limit, 1)]
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=("GET",), url_path="recent")
    def recent(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return matches played within the recent window.

        Returns:
            Response: Serialized list of recent matches.

        """
        window = timezone.now() - timedelta(days=7)
        queryset = (
            self
            .get_queryset()
            .filter(
                start_time__gte=window,
                start_time__lte=timezone.now(),
                tracker_data__status="finished",
            )
            .order_by("-start_time")[:5]
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=("GET",), url_path="finished")
    def finished(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return the latest finished matches as match summaries.

        This endpoint is designed for public/anonymous UIs (Home page, etc.)
        that want to show the latest results with scores.

        Query params:
            limit: Maximum number of matches to return (default: 3).

        Returns:
            Response: List of match summary dictionaries.

        """
        limit_param = request.query_params.get("limit")
        try:
            limit = int(limit_param) if limit_param else 3
        except ValueError:
            limit = 3

        limit = max(limit, 1)

        if self._is_cacheable_public_request():
            cache_key = self._public_cache_key()
            cache_miss = object()
            cached_payload = cache.get(cache_key, cache_miss)
            if cached_payload is not cache_miss:
                return Response(cached_payload)

        # Respect the same filtering as other match list endpoints.
        # Instead of building an IN(subquery) over matches, apply the filter
        # directly to the MatchData join to keep the query planner happy.
        now = timezone.now()
        match_filter = Q(match_link__start_time__lte=now)

        team_ids = uuid_query_values(
            request.query_params.getlist("team"), parameter="team"
        )
        club_ids = uuid_query_values(
            request.query_params.getlist("club"), parameter="club"
        )
        season_ids = uuid_query_values(
            request.query_params.getlist("season"), parameter="season"
        )

        if not team_ids and request.query_params.get("followed"):
            player = self._get_player()
            if player:
                team_ids = list(player.team_follow.values_list("id_uuid", flat=True))

        if team_ids:
            match_filter &= Q(match_link__home_team__id_uuid__in=team_ids) | Q(
                match_link__away_team__id_uuid__in=team_ids
            )

        if club_ids:
            match_filter &= Q(match_link__home_team__club__id_uuid__in=club_ids) | Q(
                match_link__away_team__club__id_uuid__in=club_ids
            )

        if season_ids:
            match_filter &= Q(match_link__season__id_uuid=season_ids[-1])

        match_data_queryset = (
            MatchData.objects
            .select_related(
                "match_link",
                "match_link__home_team__club",
                "match_link__away_team__club",
                "match_link__season",
            )
            .filter(match_filter, status="finished")
            .order_by("-match_link__start_time")[:limit]
        )

        summaries = build_match_summaries(list(match_data_queryset))
        if self._is_cacheable_public_request():
            cache.set(self._public_cache_key(), summaries, timeout=30)
        return Response(summaries)
