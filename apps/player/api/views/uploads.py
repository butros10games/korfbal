"""Upload endpoints for player media."""

from __future__ import annotations

from typing import Any, ClassVar

from django.core.files.uploadedfile import UploadedFile
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.player.api.serializers import PlayerSerializer
from apps.player.models.player import Player
from apps.player.services.goal_song import (
    sanitize_uploaded_filename,
    store_goal_song_upload_best_effort,
)

from .common import PLAYER_NOT_FOUND_MESSAGE


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

        uploaded = files[0]
        if not (isinstance(uploaded, UploadedFile) or hasattr(uploaded, "name")):
            return Response(
                {"error": "Invalid uploaded file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            player = Player.objects.get(user=request.user)
        except Player.DoesNotExist:
            return Response(
                {"error": PLAYER_NOT_FOUND_MESSAGE},
                status=status.HTTP_404_NOT_FOUND,
            )

        filename = getattr(uploaded, "name", "profile_picture")
        player.profile_picture.save(filename, uploaded)
        return Response({"url": player.get_profile_picture()})


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

        uploaded = files[0]
        if not (isinstance(uploaded, UploadedFile) or hasattr(uploaded, "name")):
            return Response(
                {"error": "Invalid uploaded file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        content_type = getattr(uploaded, "content_type", "") or ""
        allowed_types = {
            "audio/mpeg",
            "audio/mp3",
            "audio/wav",
            "audio/x-wav",
            "audio/ogg",
            "audio/mp4",
            "audio/x-m4a",
        }
        if content_type and content_type.lower() not in allowed_types:
            return Response(
                {
                    "error": "Unsupported audio type",
                    "content_type": content_type,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            player = Player.objects.get(user=request.user)
        except Player.DoesNotExist:
            return Response(
                {"error": PLAYER_NOT_FOUND_MESSAGE},
                status=status.HTTP_404_NOT_FOUND,
            )

        filename = str(getattr(uploaded, "name", "goal_song") or "goal_song")
        safe_name = sanitize_uploaded_filename(filename, fallback="goal_song")

        _, url = store_goal_song_upload_best_effort(
            player=player,
            uploaded=uploaded,
            safe_name=safe_name,
            clip_duration_seconds=8,
        )

        player.goal_song_uri = url
        player.save(update_fields=["goal_song_uri"])

        return Response({
            "url": url,
            "player": PlayerSerializer(player, context={"request": request}).data,
        })
