"""Authenticated, paginated read APIs served exclusively from the local catalogue."""

from __future__ import annotations

from typing import ClassVar

from django.db.models import Prefetch, Q, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.competition.models import (
    Allocation,
    Club,
    Match,
    Pool,
    SyncResource,
    Team,
    TeamGroup,
)
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.schedule.models import Season

from .serializers import (
    AllocationSerializer,
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
    field_filters: ClassVar = {"local_club": "local_club_id"}


class TeamViewSet(CatalogueViewSet):
    """Browse teams by season, club and sport."""

    queryset = Team.objects.select_related("season", "group").order_by("name", "pk")
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

    queryset = (
        TeamGroup.objects
        .select_related("club", "season")
        .prefetch_related(
            Prefetch(
                "variants", queryset=Team.objects.select_related("season", "group")
            )
        )
        .order_by("name", "pk")
    )
    serializer_class = CompetitionTeamGroupSerializer
    search_fields = ("name",)
    field_filters: ClassVar = {
        "season": "season_id",
        "club": "club_id",
        "local_team": "local_team_id",
    }

    def get_queryset(self) -> QuerySet:
        """Include joint teams stored locally under a partnership club."""
        query = super().get_queryset()
        local_club = self.request.query_params.get("local_club")
        if local_club:
            query = query.filter(
                Q(club__local_club_id=local_club) | Q(local_team__club_id=local_club)
            )
        return query


class PoolViewSet(CatalogueViewSet):
    """Browse poules and their official standings."""

    queryset = (
        Pool.objects
        .select_related("competition_class__edition")
        .prefetch_related(
            Prefetch(
                "entries__team", queryset=Team.objects.select_related("season", "group")
            ),
        )
        .order_by("class_name", "name", "pk")
    )
    serializer_class = CompetitionPoolSerializer
    search_fields = ("name", "class_name")
    field_filters: ClassVar = {
        "season": "season_id",
        "sport": "sport",
        "team": "entries__team_id",
        "team_group": "entries__team__group_id",
        "local_pool": "local_pool_id",
        "local_team": "entries__team__group__local_team_id",
        "category": "competition_class__category",
        "class_code": "competition_class__code",
        "age_group": "competition_class__age_group",
        "discipline": "competition_class__edition__discipline",
        "phase": "competition_class__edition__phase",
        "gender": "competition_class__edition__gender",
        "mapping_status": "mapping_status",
    }

    def get_queryset(self) -> QuerySet:
        """Return each poule once when several variants share its membership."""
        query = super().get_queryset()
        local_club = self.request.query_params.get("local_club")
        if local_club:
            query = query.filter(
                Q(entries__team__club__local_club_id=local_club)
                | Q(entries__team__group__local_team__club_id=local_club)
            )
        return query.distinct()

    @extend_schema(responses=CompetitionPoolEntrySerializer(many=True))
    @action(detail=True, methods=("get",))
    def standings(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Return official positions, retaining missing values."""
        pool = self.get_object()
        rows = pool.entries.select_related("team__season", "team__group").order_by(
            "standing__Position", "team_id"
        )
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(
            CompetitionPoolEntrySerializer(page, many=True).data
        )


class MatchViewSet(CatalogueViewSet):
    """Browse fixtures/results for analysis without contacting Sportlink."""

    queryset = Match.objects.select_related("home_team", "away_team").order_by(
        "starts_at", "external_id"
    )
    serializer_class = CompetitionMatchSerializer
    field_filters: ClassVar = {
        "season": "season_id",
        "pool": "pool_id",
        "local_match": "local_match_id",
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
            ("local_team", "__group__local_team"),
            ("local_club", "__club__local_club"),
        ):
            value = self.request.query_params.get(name)
            if value:
                sides = Q(**{f"home_team{relation}_id": value}) | Q(**{
                    f"away_team{relation}_id": value
                })
                if name == "local_club":
                    sides |= Q(home_team__group__local_team__club_id=value) | Q(
                        away_team__group__local_team__club_id=value
                    )
                query = query.filter(sides)
        return query


class ResourceViewSet(CatalogueViewSet):
    """Expose freshness and discovery progress alongside imported data."""

    queryset = SyncResource.objects.order_by("kind", "source_id", "pk")
    serializer_class = CompetitionResourceSerializer
    field_filters: ClassVar = {"season": "season_id"}


class SeasonViewSet(CatalogueViewSet):
    """List the seasons available to query."""

    queryset = Season.objects.prefetch_related("competitionedition_set").order_by(
        "-start_date", "pk"
    )
    serializer_class = CompetitionSeasonSerializer


class AllocationViewSet(CatalogueViewSet):
    """Browse staged allocations even while their provider identities are unresolved."""

    queryset = Allocation.objects.select_related("source").order_by(
        "pool_name", "team_name", "pk"
    )
    serializer_class = AllocationSerializer
    search_fields = ("team_name", "pool_name", "city")
    field_filters: ClassVar = {
        "season": "source__season_id",
        "local_team": "entry__team__group__local_team_id",
        "pool": "entry__pool_id",
        "gender": "classification__gender",
        "discipline": "classification__discipline",
        "phase": "classification__phase",
        "category": "classification__category",
        "age_group": "classification__age_group",
        "class_code": "classification__code",
    }

    def get_queryset(self) -> QuerySet:
        """Include allocations of native partnership teams under either club link."""
        query = super().get_queryset()
        local_club = self.request.query_params.get("local_club")
        if local_club:
            query = query.filter(
                Q(entry__team__club__local_club_id=local_club)
                | Q(entry__team__group__local_team__club_id=local_club)
            )
        return query
