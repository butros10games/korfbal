"""Views for hub API endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, ClassVar

from django.http import HttpRequest
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.schedule.models import Match

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
            Match.objects.select_related(
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
