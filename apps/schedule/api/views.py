"""Views for schedule endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, ClassVar

from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.player.models.player import Player
from apps.schedule.models import Match

from .serializers import MatchSerializer


class MatchViewSet(viewsets.ReadOnlyModelViewSet):
    """Expose match data for the mobile frontend."""

    serializer_class = MatchSerializer
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get_queryset(self) -> QuerySet[Match]:
        """Return a queryset filtered by the current request context.

        Returns:
            QuerySet[Match]: Filtered match queryset.

        """
        queryset = Match.objects.select_related(
            "home_team__club",
            "away_team__club",
            "season",
        ).order_by("start_time")

        team_ids = self.request.query_params.getlist("team")
        club_ids = self.request.query_params.getlist("club")
        season_id = self.request.query_params.get("season")

        if not team_ids and self.request.query_params.get("followed"):
            player = self._get_player()
            if player:
                team_ids = list(player.team_follow.values_list("id_uuid", flat=True))

        if team_ids:
            queryset = queryset.filter(
                Q(home_team__id_uuid__in=team_ids) | Q(away_team__id_uuid__in=team_ids)
            )

        if club_ids:
            queryset = queryset.filter(
                Q(home_team__club__id_uuid__in=club_ids)
                | Q(away_team__club__id_uuid__in=club_ids)
            )

        if season_id:
            queryset = queryset.filter(season__id_uuid=season_id)

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
                    Player.objects.prefetch_related("team_follow")
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

    @action(detail=False, methods=["GET"], url_path="next")  # type: ignore[arg-type]
    def next_match(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the next upcoming match for the active context.

        Returns:
            Response: Serialized next match.

        """
        match = self._upcoming_queryset().first()
        if not match:
            return Response(None, status=status.HTTP_200_OK)
        serializer = self.get_serializer(match)
        return Response(serializer.data)

    @action(detail=False, methods=["GET"], url_path="upcoming")  # type: ignore[arg-type]
    def upcoming(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
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

    @action(detail=False, methods=["GET"], url_path="recent")  # type: ignore[arg-type]
    def recent(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return matches played within the recent window.

        Returns:
            Response: Serialized list of recent matches.

        """
        window = timezone.now() - timedelta(days=7)
        queryset = (
            self.get_queryset()
            .filter(start_time__gte=window)
            .order_by("-start_time")[:5]
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
