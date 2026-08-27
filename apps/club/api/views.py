"""ViewSets for club endpoints."""

from __future__ import annotations

from typing import Any

from django.db import models
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.club.models.club import Club
from apps.club.queries.overview import (
    club_matches,
    club_seasons,
    club_teams,
)
from apps.club.services.admin import (
    close_active_membership,
    create_active_membership,
    get_club_admin_settings_data,
    resolve_player_for_membership,
    search_club_admin_users,
)
from apps.club.services.eligibility_dashboard import build_club_eligibility_dashboard
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.api.permissions import IsStaffOrReadOnly
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.schedule.queries.seasons import (
    requested_or_default_season,
    season_options_payload,
)
from apps.team.api.serializers import TeamSerializer

from .permissions import IsClubAdmin
from .serializers import (
    ClubAdminPlayerSerializer,
    ClubMembershipAddSerializer,
    ClubMembershipSerializer,
    ClubSerializer,
)


@extend_schema_view(
    remove_membership=extend_schema(
        parameters=[
            OpenApiParameter("player_id", OpenApiTypes.UUID, OpenApiParameter.PATH)
        ]
    )
)
class ClubViewSet(viewsets.ModelViewSet):
    """Expose club CRUD endpoints with search support."""

    queryset = Club.objects.order_by("name", "id_uuid").fetch_mode(models.FETCH_RAISE)
    serializer_class = ClubSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = (IsStaffOrReadOnly,)
    lookup_field = "id_uuid"
    filter_backends = (filters.SearchFilter,)
    search_fields = ("name",)

    @action(detail=True, methods=("GET",), url_path="overview")
    def overview(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Return teams and match summaries for a club detail page.

        Returns:
            Response: JSON payload with club overview data.

        """
        club = self.get_object()
        seasons_qs = list(club_seasons(club))
        season = requested_or_default_season(
            request.query_params.get("season"), seasons_qs
        )

        teams_qs = club_teams(club, season)
        teams_payload = TeamSerializer(
            teams_qs,
            many=True,
            context=self.get_serializer_context(),
        ).data

        match_data_qs = club_matches(club, season)

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

        payload = {
            "club": self.get_serializer(club).data,
            "teams": teams_payload,
            "matches": {
                "upcoming": upcoming_matches,
                "recent": recent_matches,
            },
            "seasons": season_options_payload(seasons_qs),
            "meta": {
                "team_count": len(teams_payload),
                "season_id": str(season.id_uuid) if season else None,
                "season_name": season.name if season else None,
                "viewer_is_admin": self._viewer_is_admin(request, club),
            },
        }
        return Response(payload)

    @action(
        detail=True,
        methods=("GET",),
        url_path="settings",
        permission_classes=[permissions.IsAuthenticated, IsClubAdmin],
    )
    def admin_settings(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Return data needed for the club admin settings screen."""
        club = self.get_object()
        admins, memberships = get_club_admin_settings_data(club=club)

        payload = {
            "club": self.get_serializer(club).data,
            "admins": [
                ClubAdminPlayerSerializer().to_representation(p) for p in admins
            ],
            "members": [
                ClubMembershipSerializer().to_representation(m) for m in memberships
            ],
        }

        return Response(payload)

    @action(
        detail=True,
        methods=("GET",),
        url_path="settings/user-search",
        permission_classes=[permissions.IsAuthenticated, IsClubAdmin],
    )
    def user_search(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Search users/players by username for adding club memberships."""
        term = (request.query_params.get("search") or "").strip()
        return Response({"results": search_club_admin_users(term=term)})

    @action(
        detail=True,
        methods=("GET",),
        url_path="eligibility-dashboard",
        permission_classes=[permissions.IsAuthenticated, IsClubAdmin],
    )
    def eligibility_dashboard(
        self,
        request: Request,
        **kwargs: object,
    ) -> Response:
        """Return club-level player eligibility/vastspelen dashboard data."""
        club = self.get_object()
        seasons_qs = list(club_seasons(club))
        season = requested_or_default_season(
            request.query_params.get("season"), seasons_qs
        )
        return Response(
            build_club_eligibility_dashboard(
                club=club,
                season=season,
            )
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="memberships",
        permission_classes=[permissions.IsAuthenticated, IsClubAdmin],
    )
    def add_membership(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Add a player/user to the club by creating an active membership."""
        club = self.get_object()

        serializer = ClubMembershipAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        player = resolve_player_for_membership(data)
        if player is None:
            return Response(
                {"detail": "Player/user not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership, created = create_active_membership(
            club=club,
            player=player,
            start_date=data.get("start_date"),
        )
        if not created:
            return Response(
                {"detail": "Player is already an active member of this club."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            ClubMembershipSerializer().to_representation(membership),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("DELETE",),
        url_path=r"memberships/(?P<player_id>[^/.]+)",
        permission_classes=[permissions.IsAuthenticated, IsClubAdmin],
    )
    def remove_membership(
        self,
        request: Request,
        player_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Remove a player from the club by closing their active membership."""
        club = self.get_object()
        if not close_active_membership(club=club, player_id=player_id):
            return Response(
                {"detail": "Active membership not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)

    def _viewer_is_admin(self, request: Request, club: Club) -> bool:
        user = request.user
        if not user.is_authenticated:
            return False
        return club.admin.filter(user=user).exists()
