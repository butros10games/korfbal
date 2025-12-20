"""Views for hub API endpoints."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any, ClassVar

from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.game_tracker.models import MatchData, Shot
from apps.schedule.models import Match
from apps.team.models import TeamData

from .serializers import UpdateSerializer


class UpdateFeedView(APIView):
    """Return a lightweight feed of match-centric updates."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: HttpRequest,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return serialized updates derived from upcoming matches.

        Returns:
            Response: Serialized update data.

        """
        now = timezone.now()
        match_window_start = now - timedelta(days=7)

        queryset = (
            Match.objects
            .select_related(
                "home_team__club",
                "away_team__club",
                "season",
            )
            .filter(start_time__gte=match_window_start)
            .order_by("-start_time")
        )

        matches = list(queryset[:10])
        if not matches:
            matches = list(
                Match.objects.select_related(
                    "home_team__club",
                    "away_team__club",
                    "season",
                ).order_by("-start_time")[:5]
            )

        updates: list[dict[str, Any]] = [
            {
                "id": str(match.id_uuid),
                "title": (
                    f"{match.home_team.club.name} {match.home_team.name} vs "
                    f"{match.away_team.club.name} {match.away_team.name}"
                ),
                "description": (
                    f"{match.season.name} · "
                    f"{match.start_time.strftime('%a %d %b %H:%M')}"
                ),
                "timestamp": match.start_time,
            }
            for match in matches
        ]

        serializer = UpdateSerializer(updates, many=True)
        return Response(serializer.data)


HTTP_STATUS_OK = 200


def _payload_match(match: Match) -> dict[str, Any]:
    return {
        "id_uuid": str(match.id_uuid),
        "home_team_id": str(match.home_team.id_uuid),
        "away_team_id": str(match.away_team.id_uuid),
        "start_time": match.start_time.isoformat(),
    }


def _payload_match_data(match_data: MatchData) -> dict[str, Any]:
    return {
        "id_uuid": str(match_data.id_uuid),
        "status": match_data.status,
    }


class HubIndexView(APIView):
    """Return hub landing-page data for the authenticated user.

    The old Django-rendered hub index page was removed in favor of a React SPA.
    This endpoint provides the minimal data that the hub index tests expect:
    - the next relevant match (or active match)
    - match_data when available
    - scores for active matches

    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return hub index data for the authenticated user."""
        user = request.user

        # Some environments may not create Player profiles automatically.
        player = getattr(user, "player", None)
        if player is None:
            return Response(
                {
                    "match": None,
                    "match_data": None,
                    "home_score": 0,
                    "away_score": 0,
                },
                status=HTTP_STATUS_OK,
            )

        team_ids = list(
            TeamData.objects.filter(players=player).values_list("team_id", flat=True)
        )
        if not team_ids:
            return Response(
                {
                    "match": None,
                    "match_data": None,
                    "home_score": 0,
                    "away_score": 0,
                },
                status=HTTP_STATUS_OK,
            )

        # Prefer an active match for the user's teams.
        active_match_data = (
            MatchData.objects
            .select_related(
                "match_link__home_team",
                "match_link__away_team",
            )
            .filter(status="active")
            .filter(
                Q(match_link__home_team_id__in=team_ids)
                | Q(match_link__away_team_id__in=team_ids)
            )
            .order_by("-match_link__start_time")
            .first()
        )
        if active_match_data is not None:
            match = active_match_data.match_link
            home_score = Shot.objects.filter(
                match_data=active_match_data,
                team=match.home_team,
                scored=True,
            ).count()
            away_score = Shot.objects.filter(
                match_data=active_match_data,
                team=match.away_team,
                scored=True,
            ).count()
            return Response(
                {
                    "match": _payload_match(match),
                    "match_data": _payload_match_data(active_match_data),
                    "home_score": home_score,
                    "away_score": away_score,
                },
                status=HTTP_STATUS_OK,
            )

        now = timezone.now()
        upcoming_match = (
            Match.objects
            .select_related(
                "home_team",
                "away_team",
            )
            .filter(
                Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids),
                start_time__gte=now,
            )
            .order_by("start_time")
            .first()
        )
        if upcoming_match is not None:
            match_data, _created = MatchData.objects.get_or_create(
                match_link=upcoming_match,
            )
            return Response(
                {
                    "match": _payload_match(upcoming_match),
                    "match_data": _payload_match_data(match_data),
                    "home_score": None,
                    "away_score": None,
                },
                status=HTTP_STATUS_OK,
            )

        return Response(
            {
                "match": None,
                "match_data": None,
                "home_score": 0,
                "away_score": 0,
            },
            status=HTTP_STATUS_OK,
        )


class CatalogDataView(APIView):
    """Return catalog data for hub pickers.

    Legacy code used a server-side endpoint named `api_catalog_data`. The SPA
    should eventually call dedicated endpoints per resource; for now we keep a
    minimal compatibility endpoint that returns empty lists when a user has no
    Player profile.
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return catalog data for the given selector payload."""
        # Be lenient about payload encoding (tests send JSON string bodies).
        payload: dict[str, Any]
        if isinstance(request.data, dict):
            payload = request.data
        else:
            try:
                parsed = json.loads(request.body)
            except Exception:
                parsed = {}
            payload = parsed if isinstance(parsed, dict) else {}

        value = str(payload.get("value") or "")
        # Older client shape
        return Response(
            {
                "type": value,
                "connected": [],
                "following": [],
            },
            status=HTTP_STATUS_OK,
        )
