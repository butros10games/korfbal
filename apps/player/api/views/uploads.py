"""Upload endpoints for player media."""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.player.api.serializers import PlayerSerializer
from apps.player.services.player_uploads import (
    goal_song_content_type_allowed,
    save_goal_song_upload,
    save_profile_picture_upload,
    uploaded_file_or_none,
)

from .common import PLAYER_NOT_FOUND_MESSAGE, get_current_player


class UploadProfilePictureAPIView(APIView):
    """Upload a profile picture (API variant used by the Vite frontend)."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]
    parser_classes: ClassVar[list[type[Any]]] = [MultiPartParser, FormParser]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Upload and persist a profile picture for the authenticated player."""
        files = request.FILES.getlist("profile_picture")
        if not files:
            return Response(
                {"error": "No profile_picture uploaded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded = uploaded_file_or_none(files[0])
        if uploaded is None:
            return Response(
                {"error": "Invalid uploaded file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        player = get_current_player(request)
        if player is None:
            return Response(
                {"error": PLAYER_NOT_FOUND_MESSAGE},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({
            "url": save_profile_picture_upload(player=player, uploaded=uploaded)
        })


class UploadGoalSongAPIView(APIView):
    """Upload an audio file to use as the goal song."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]
    parser_classes: ClassVar[list[type[Any]]] = [MultiPartParser, FormParser]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Upload an audio file and store its URL on the authenticated player."""
        files = request.FILES.getlist("goal_song")
        if not files:
            return Response(
                {"error": "No goal_song uploaded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded = uploaded_file_or_none(files[0])
        if uploaded is None:
            return Response(
                {"error": "Invalid uploaded file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not goal_song_content_type_allowed(uploaded):
            content_type = getattr(uploaded, "content_type", "") or ""
            return Response(
                {
                    "error": "Unsupported audio type",
                    "content_type": content_type,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        player = get_current_player(request)
        if player is None:
            return Response(
                {"error": PLAYER_NOT_FOUND_MESSAGE},
                status=status.HTTP_404_NOT_FOUND,
            )

        url = save_goal_song_upload(
            player=player,
            uploaded=uploaded,
            clip_duration_seconds=8,
        )

        return Response({
            "url": url,
            "player": PlayerSerializer(player, context={"request": request}).data,
        })
