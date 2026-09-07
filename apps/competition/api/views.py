"""Authenticated, paginated read APIs served exclusively from the local catalogue."""

from __future__ import annotations

from typing import ClassVar

from django.db.models import Q, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.competition.models import Club, Match, Pool, SyncResource, Team, TeamGroup
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.schedule.models import Season

from .serializers import (
    CompetitionClubSerializer,
    CompetitionFilters,
    CompetitionMatchSerializer,
    CompetitionPoolEntrySerializer,
    CompetitionPoolSerializer,
    CompetitionResourceSerializer,
    CompetitionSeasonSerializer,
    CompetitionTeamGroupSerializer,
    CompetitionTeamSerializer,
)


class CatalogueViewSet(viewsets.ReadOnlyModelViewSet):
    """Share typed filters and bounded pagination across catalogue resources."""

    permission_classes = (permissions.IsAuthenticated,)
    pagination_class = StandardResultsSetPagination
    filter_backends = (filters.SearchFilter,)
    field_filters: ClassVar[dict[str, str]] = {}

    def get_queryset(self) -> QuerySet:
        """Apply only supported, validated filters for each resource."""
        query = super().get_queryset()
        validator = CompetitionFilters(data=self.request.query_params)
        validator.is_valid(raise_exception=True)
        for name, field in self.field_filters.items():
            if name in validator.validated_data:
                query = query.filter(**{field: validator.validated_data[name]})
        return query


class ClubViewSet(CatalogueViewSet):
    """Search the source club directory."""

    queryset = Club.objects.order_by("name", "pk")
    serializer_class = CompetitionClubSerializer
    search_fields = ("name", "city")


class TeamViewSet(CatalogueViewSet):
    """Browse teams by season, club and sport."""

    queryset = Team.objects.order_by("name", "pk")
    serializer_class = CompetitionTeamSerializer
    search_fields = ("name",)
    field_filters: ClassVar = {
        "season": "season_id",
        "club": "club_id",
        "team_group": "group_id",
        "sport": "sport",
    }


class TeamGroupViewSet(CatalogueViewSet):
    """Browse one team per club/season with indoor and outdoor variants together."""

    queryset = TeamGroup.objects.prefetch_related("variants").order_by("name", "pk")
    serializer_class = CompetitionTeamGroupSerializer
    search_fields = ("name",)
    field_filters: ClassVar = {"season": "season_id", "club": "club_id"}


class PoolViewSet(CatalogueViewSet):
    """Browse poules and their official standings."""

    queryset = Pool.objects.order_by("class_name", "name", "pk")
    serializer_class = CompetitionPoolSerializer
    search_fields = ("name", "class_name")
    field_filters: ClassVar = {
        "season": "season_id",
        "sport": "sport",
        "team": "entries__team_id",
    }

    @extend_schema(responses=CompetitionPoolEntrySerializer(many=True))
    @action(detail=True, methods=("get",))
    def standings(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Return official positions, retaining missing values."""
        pool = self.get_object()
        rows = pool.entries.select_related("team").order_by(
            "standing__Position", "team_id"
        )
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(
            CompetitionPoolEntrySerializer(page, many=True).data
        )


class MatchViewSet(CatalogueViewSet):
    """Browse fixtures/results for analysis without contacting Sportlink."""

    queryset = Match.objects.order_by("starts_at", "external_id")
    serializer_class = CompetitionMatchSerializer
    field_filters: ClassVar = {
        "season": "season_id",
        "pool": "pool_id",
        "sport": "home_team__sport",
        "status": "status",
        "date_from": "starts_at__gte",
        "date_to": "starts_at__lte",
    }

    def get_queryset(self) -> QuerySet:
        """Include either side when filtering by team or club."""
        query = super().get_queryset()
        for name, relation in (
            ("team", ""),
            ("club", "__club"),
            ("team_group", "__group"),
        ):
            value = self.request.query_params.get(name)
            if value:
                query = query.filter(
                    Q(**{f"home_team{relation}_id": value})
                    | Q(**{f"away_team{relation}_id": value})
                )
        return query


class ResourceViewSet(CatalogueViewSet):
    """Expose freshness and discovery progress alongside imported data."""

    queryset = SyncResource.objects.order_by("kind", "source_id", "pk")
    serializer_class = CompetitionResourceSerializer
    field_filters: ClassVar = {"season": "season_id"}


class SeasonViewSet(CatalogueViewSet):
    """List the seasons available to query."""

    queryset = Season.objects.order_by("-start_date", "pk")
    serializer_class = CompetitionSeasonSerializer
