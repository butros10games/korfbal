"""ViewSets for club endpoints."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.club.models.club import Club
from apps.game_tracker.models import MatchData
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.schedule.models import Season
from apps.team.api.serializers import TeamSerializer
from apps.team.models.team import Team

from .serializers import ClubSerializer


class ClubViewSet(viewsets.ModelViewSet):
    """Expose club CRUD endpoints with search support."""

    queryset = Club.objects.all().order_by("name")
    serializer_class = ClubSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticatedOrReadOnly,
    ]
    lookup_field = "id_uuid"
    filter_backends: ClassVar[list[type[filters.BaseFilterBackend]]] = [
        filters.SearchFilter,
    ]
    search_fields: ClassVar[list[str]] = ["name"]

    @action(detail=True, methods=["GET"], url_path="overview")  # type: ignore[arg-type]
    def overview(self, request: Request, *args: Any, **kwargs: Any) -> Response:  # noqa: ANN401
        """Return teams and match summaries for a club detail page.

        Returns:
            Response: JSON payload with club overview data.

        """
        club = self.get_object()
        seasons_qs = list(self._club_seasons_queryset(club))
        season = self._resolve_season(request, seasons_qs)

        teams_qs = self._club_teams_queryset(club, season)
        teams_payload = TeamSerializer(
            teams_qs,
            many=True,
            context=self.get_serializer_context(),
        ).data

        match_data_qs = self._club_match_queryset(club, season)

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
            "club": self.get_serializer(club).data,
            "teams": teams_payload,
            "matches": {
                "upcoming": upcoming_matches,
                "recent": recent_matches,
            },
            "seasons": seasons_payload,
            "meta": {
                "team_count": len(teams_payload),
                "season_id": str(season.id_uuid) if season else None,
                "season_name": season.name if season else None,
            },
        }
        return Response(payload)

    def _club_teams_queryset(self, club: Club, season: Season | None) -> QuerySet[Team]:
        queryset = club.teams.select_related("club").order_by("name")
        if season:
            queryset = queryset.filter(
                Q(team_data__season_id=season.id_uuid)
                | Q(home_matches__season_id=season.id_uuid)
                | Q(away_matches__season_id=season.id_uuid)
            ).distinct()
        return queryset

    def _club_match_queryset(
        self, club: Club, season: Season | None
    ) -> QuerySet[MatchData]:
        queryset = MatchData.objects.select_related(
            "match_link",
            "match_link__home_team",
            "match_link__home_team__club",
            "match_link__away_team",
            "match_link__away_team__club",
            "match_link__season",
        ).filter(
            Q(match_link__home_team__club=club) | Q(match_link__away_team__club=club),
        )
        if season:
            queryset = queryset.filter(match_link__season_id=season.id_uuid)
        return queryset

    def _resolve_season(self, request: Request, seasons: list[Season]) -> Season | None:
        season_param = request.query_params.get("season")
        if season_param:
            return Season.objects.filter(id_uuid=season_param).first()

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

    def _club_seasons_queryset(self, club: Club) -> QuerySet[Season]:
        return (
            Season.objects.filter(
                Q(team_data__team__club=club)
                | Q(matches__home_team__club=club)
                | Q(matches__away_team__club=club)
            )
            .distinct()
            .order_by("-start_date")
        )
