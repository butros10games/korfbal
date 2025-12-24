"""ViewSets for club endpoints."""

from __future__ import annotations

from typing import Any, ClassVar

from django.contrib.auth import get_user_model
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.club.models.club import Club
from apps.game_tracker.models import MatchData
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.api.permissions import IsStaffOrReadOnly
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.player.models.player import Player
from apps.player.models.player_club_membership import PlayerClubMembership
from apps.schedule.models import Season
from apps.team.api.serializers import TeamSerializer
from apps.team.models.team import Team

from .permissions import IsClubAdmin
from .serializers import (
    ClubAdminPlayerSerializer,
    ClubMembershipAddSerializer,
    ClubMembershipSerializer,
    ClubSerializer,
)


MIN_USER_SEARCH_TERM_LENGTH = 2


class ClubViewSet(viewsets.ModelViewSet):
    """Expose club CRUD endpoints with search support."""

    queryset = Club.objects.all().order_by("name")
    serializer_class = ClubSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        IsStaffOrReadOnly,
    ]
    lookup_field = "id_uuid"
    filter_backends: ClassVar[list[type[filters.BaseFilterBackend]]] = [
        filters.SearchFilter,
    ]
    search_fields: ClassVar[list[str]] = ["name"]

    @action(detail=True, methods=("GET",), url_path="overview")
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
    def admin_settings(self, request: Request, *args: Any, **kwargs: Any) -> Response:  # noqa: ANN401
        """Return data needed for the club admin settings screen."""
        club = self.get_object()

        admins = list(club.admin.select_related("user").order_by("user__username"))

        today = timezone.localdate()
        memberships = list(
            PlayerClubMembership.objects
            .select_related("player", "player__user")
            .filter(
                club=club,
                start_date__lte=today,
            )
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .order_by("player__user__username")
        )

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
    def user_search(self, request: Request, *args: Any, **kwargs: Any) -> Response:  # noqa: ANN401
        """Search users/players by username for adding club memberships."""
        term = (request.query_params.get("search") or "").strip()
        if len(term) < MIN_USER_SEARCH_TERM_LENGTH:
            return Response({"results": []})

        user_model = get_user_model()
        users = (
            user_model.objects
            .filter(username__icontains=term)
            .order_by("username")
            .only("id", "username")[:20]
        )

        players_by_user_id = {
            player.user_id: player
            for player in Player.objects.filter(user__in=users).select_related("user")
        }

        results: list[dict[str, object]] = []
        for user in users:
            player = players_by_user_id.get(user.id)
            results.append({
                "user_id": user.id,
                "username": user.username,
                "player_id": str(player.id_uuid) if player else None,
            })

        return Response({"results": results})

    @action(
        detail=True,
        methods=("POST",),
        url_path="memberships",
        permission_classes=[permissions.IsAuthenticated, IsClubAdmin],
    )
    def add_membership(self, request: Request, *args: Any, **kwargs: Any) -> Response:  # noqa: ANN401
        """Add a player/user to the club by creating an active membership."""
        club = self.get_object()

        serializer = ClubMembershipAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        player = self._resolve_player_for_membership(data)
        if player is None:
            return Response(
                {"detail": "Player/user not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_date = data.get("start_date") or timezone.localdate()

        membership, created = PlayerClubMembership.objects.get_or_create(
            player=player,
            club=club,
            end_date__isnull=True,
            defaults={"start_date": start_date},
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
        today = timezone.localdate()
        membership = (
            PlayerClubMembership.objects
            .filter(
                club=club,
                player_id=player_id,
                end_date__isnull=True,
            )
            .order_by("-start_date")
            .first()
        )
        if membership is None:
            return Response(
                {"detail": "Active membership not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        membership.end_date = today
        membership.save(update_fields=["end_date"])

        return Response(status=status.HTTP_204_NO_CONTENT)

    def _viewer_is_admin(self, request: Request, club: Club) -> bool:
        viewer = self._viewer_player(request)
        if viewer is None:
            return False
        return club.admin.filter(id_uuid=viewer.id_uuid).exists()

    def _viewer_player(self, request: Request) -> Player | None:
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return Player.objects.filter(user=user).first()

    def _resolve_player_for_membership(self, data: dict[str, Any]) -> Player | None:
        player_id = data.get("player_id")
        if player_id:
            return (
                Player.objects.filter(id_uuid=player_id).select_related("user").first()
            )

        user_model = get_user_model()
        user_id = data.get("user_id")
        username = data.get("username")
        if user_id:
            user = user_model.objects.filter(id=user_id).first()
        elif username:
            user = user_model.objects.filter(username__iexact=username).first()
        else:
            user = None

        if user is None:
            return None

        player, _ = Player.objects.get_or_create(user=user)
        return (
            Player.objects.filter(id_uuid=player.id_uuid).select_related("user").first()
        )

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
        """Resolve the requested season in a safe, club-scoped way.

        Important:
            If a `season` query param is supplied but cannot be resolved within
            the club's known seasons, we do **not** return `None` (which would
            broaden queries to all seasons). Instead, we fall back to a sensible
            default within the provided season list.

        """
        season_param = request.query_params.get("season")
        if season_param:
            selected = next(
                (option for option in seasons if str(option.id_uuid) == season_param),
                None,
            )
            if selected is not None:
                return selected

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
            Season.objects
            .filter(
                Q(team_data__team__club=club)
                | Q(matches__home_team__club=club)
                | Q(matches__away_team__club=club)
            )
            .distinct()
            .order_by("-start_date")
        )
