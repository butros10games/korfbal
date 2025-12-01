"""Views powering the player API endpoints."""

from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from rest_framework import permissions, status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.player.models.player import Player

from .serializers import PlayerSerializer


class PlayerViewSet(viewsets.ModelViewSet):
    """Provide CRUD operations for players."""

    queryset = Player.objects.select_related("user").prefetch_related(
        "team_follow",
        "club_follow",
    )
    serializer_class = PlayerSerializer
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticatedOrReadOnly,
    ]
    lookup_field = "id_uuid"


class CurrentPlayerAPIView(APIView):
    """Return the profile for the active player (or a debug fallback)."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(  # noqa: PLR6301
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the current player's profile.

        Args:
            request (Request): The request object.
            *args (Any): Positional arguments.
            **kwargs (Any): Keyword arguments.

        Returns:
            Response: The serialized player profile.

        """
        queryset = Player.objects.select_related("user").prefetch_related(
            "team_follow",
            "club_follow",
        )

        player = None
        if request.user.is_authenticated:
            try:
                player = queryset.get(user=request.user)
            except Player.DoesNotExist:
                player = None

        if player is None and settings.DEBUG:
            player_id = request.query_params.get("player_id")
            if player_id:
                player = queryset.filter(id_uuid=player_id).first()
            if player is None:
                player = queryset.first()

        if player is None:
            return Response(
                {"detail": "Player not found"}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = PlayerSerializer(player)
        return Response(serializer.data)
