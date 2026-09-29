"""Team roster reads and membership changes."""

from __future__ import annotations

from typing import Any

from django.db import models
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.params import uuid_query_value
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.api.permissions import (
    viewer_can_manage_roster,
)
from apps.team.models.team_data import TeamData
from apps.team.services.roster import change_team_membership

from .serializers import (
    TeamRosterMutationSerializer,
)


_ROSTER_SEARCH_MIN_LENGTH = 2
_ROSTER_SEARCH_LIMIT = 20


class TeamRosterActions(viewsets.GenericViewSet):
    """Read and change a team season's roster."""

    @action(
        detail=True,
        methods=("GET", "PATCH"),
        url_path="roster",
        permission_classes=[permissions.AllowAny],
    )
    def roster(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Read season membership or add/remove one player without replacing the roster.

        Raises:
            ValidationError: If the season or mutation is invalid.
            PermissionDenied: If the viewer cannot manage this season's team.
            NotFound: If the season does not exist.

        """
        team = self.get_object()
        season_id = request.query_params.get("season")
        if not season_id:
            raise ValidationError({"season": "Select a season."})
        season = Season.objects.filter(
            id_uuid=uuid_query_value(season_id, parameter="season")
        ).first()
        if season is None:
            raise NotFound("Season not found.")
        can_manage = viewer_can_manage_roster(request=request, team=team, season=season)
        if request.method == "PATCH":
            if not can_manage:
                raise PermissionDenied("You cannot manage this team's players.")
            serializer = TeamRosterMutationSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            change_team_membership(
                team=team, season=season, **serializer.validated_data
            )
        players = (
            Player.objects
            .filter(team_data_as_player__team=team, team_data_as_player__season=season)
            .select_related("user")
            .distinct()
            .order_by("user__username", "name", "id_uuid")
        )
        return Response({
            "can_manage": can_manage,
            "players": [
                {"id_uuid": str(player.id_uuid), "username": player.display_name}
                for player in players
            ],
        })

    @action(
        detail=True,
        methods=("GET",),
        url_path="roster-candidates",
        permission_classes=[permissions.IsAuthenticated],
        filter_backends=[],
    )
    def roster_candidates(
        self, request: Request, *args: Any, **kwargs: Any
    ) -> Response:
        """Search existing profiles for managers of the selected season.

        Raises:
            ValidationError: If no valid season is selected.
            PermissionDenied: If the viewer cannot manage this season's team.
            NotFound: If the season does not exist.

        """
        team = self.get_object()
        season_id = request.query_params.get("season")
        if not season_id:
            raise ValidationError({"season": "Select a season."})
        season = Season.objects.filter(
            id_uuid=uuid_query_value(season_id, parameter="season")
        ).first()
        if season is None:
            raise NotFound("Season not found.")
        if not viewer_can_manage_roster(request=request, team=team, season=season):
            raise PermissionDenied("You cannot manage this team's players.")
        search = request.query_params.get("search", "").strip()
        if len(search) < _ROSTER_SEARCH_MIN_LENGTH:
            return Response({"players": [], "has_more": False})
        linked_ids = TeamData.players.through.objects.filter(
            teamdata__team=team, teamdata__season=season
        ).values_list("player_id", flat=True)
        candidates = list(
            Player.objects
            .select_related("user")
            .filter(
                models.Q(user__username__icontains=search)
                | models.Q(name__icontains=search)
            )
            .exclude(id_uuid__in=linked_ids)
            .order_by("user__username", "name", "id_uuid")[: _ROSTER_SEARCH_LIMIT + 1]
        )
        return Response({
            "players": [
                {"id_uuid": str(player.id_uuid), "username": player.display_name}
                for player in candidates[:_ROSTER_SEARCH_LIMIT]
            ],
            "has_more": len(candidates) > _ROSTER_SEARCH_LIMIT,
        })
