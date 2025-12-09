"""ViewSets for team-related API endpoints."""

from __future__ import annotations

from typing import Any, ClassVar

from asgiref.sync import async_to_sync
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import MatchData
from apps.kwt_common.utils.general_stats import build_general_stats
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.kwt_common.utils.players_stats import build_player_stats
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models.team import Team

from .serializers import TeamSerializer


class TeamViewSet(viewsets.ModelViewSet):
    """Expose team CRUD endpoints with lightweight search support."""

    queryset = Team.objects.select_related("club").order_by("club__name", "name")
    serializer_class = TeamSerializer
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticatedOrReadOnly,
    ]
    lookup_field = "id_uuid"
    filter_backends: ClassVar[list[type[filters.BaseFilterBackend]]] = [
        filters.SearchFilter
    ]
    search_fields: ClassVar[list[str]] = ["name", "club__name"]

    @action(detail=True, methods=["GET"], url_path="overview")  # type: ignore[arg-type]
    def overview(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return match summaries, stats, roster data, and season options.

        Returns:
            Response: Aggregated team overview data.

        """
        team = self.get_object()
        season = self._resolve_season(request)

        match_data_qs = self._team_match_queryset(team, season)
        upcoming_matches = build_match_summaries(
            match_data_qs.filter(status__in=["upcoming", "active"]).order_by(
                "match_link__start_time"
            )[:10]
        )
        recent_matches = build_match_summaries(
            match_data_qs.filter(status="finished").order_by("-match_link__start_time")[
                :10
            ]
        )

        full_match_dataset = list(match_data_qs)
        stats_general = (
            async_to_sync(build_general_stats)(full_match_dataset)
            if full_match_dataset
            else None
        )

        roster_players = list(self._team_players_queryset(team, season, match_data_qs))
        roster = [
            {
                "id_uuid": str(player.id_uuid),
                "display_name": player.user.get_full_name() or player.user.username,
                "username": player.user.username,
                "profile_picture_url": player.get_profile_picture(),
                "profile_url": player.get_absolute_url(),
            }
            for player in roster_players
        ]

        stats_players = (
            async_to_sync(build_player_stats)(roster_players, full_match_dataset)
            if roster and full_match_dataset
            else []
        )

        seasons_qs = list(self._team_seasons_queryset(team))
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
            "team": self.get_serializer(team).data,
            "matches": {
                "upcoming": upcoming_matches,
                "recent": recent_matches,
            },
            "stats": {
                "general": stats_general,
                "players": stats_players,
            },
            "roster": roster,
            "seasons": seasons_payload,
            "meta": {
                "season_id": str(season.id_uuid) if season else None,
                "season_name": season.name if season else None,
                "roster_count": len(roster),
            },
        }
        return Response(payload)

    def _team_match_queryset(
        self, team: Team, season: Season | None
    ) -> QuerySet[MatchData]:
        queryset = MatchData.objects.select_related(
            "match_link",
            "match_link__home_team",
            "match_link__home_team__club",
            "match_link__away_team",
            "match_link__away_team__club",
            "match_link__season",
        ).filter(
            Q(match_link__home_team=team) | Q(match_link__away_team=team),
        )
        if season:
            queryset = queryset.filter(match_link__season=season)
        return queryset

    def _resolve_season(self, request: Request) -> Season | None:
        season_param = request.query_params.get("season")
        if season_param:
            return Season.objects.filter(id_uuid=season_param).first()

        return self._current_season() or self._most_recent_season()

    def _current_season(self) -> Season | None:
        today = timezone.now().date()
        return Season.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
        ).first()

    def _most_recent_season(self) -> Season | None:
        today = timezone.now().date()
        return Season.objects.filter(end_date__lte=today).order_by("-end_date").first()

    def _team_players_queryset(
        self, team: Team, season: Season | None, match_data_qs: QuerySet[MatchData]
    ) -> QuerySet[Player]:
        queryset = Player.objects.select_related("user").filter(
            Q(team_data_as_player__team=team)
            | Q(
                match_players__team=team,
                match_players__match_data__in=match_data_qs,
            )
            | Q(shots__team=team, shots__match_data__in=match_data_qs)
        )

        if season:
            queryset = queryset.filter(
                Q(team_data_as_player__season=season)
                | Q(match_players__match_data__match_link__season=season)
                | Q(shots__match_data__match_link__season=season)
            )

        return queryset.distinct().order_by("user__username")

    def _team_seasons_queryset(self, team: Team) -> QuerySet[Season]:
        return (
            Season.objects.filter(
                Q(team_data__team=team)
                | Q(matches__home_team=team)
                | Q(matches__away_team=team)
            )
            .distinct()
            .order_by("-start_date")
        )
