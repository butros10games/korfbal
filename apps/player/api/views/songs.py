"""Song download and clip API views."""

from __future__ import annotations

from typing import Any, ClassVar

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import HttpResponseRedirect
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.player.api.serializers import (
    PlayerSongCreateSerializer,
    PlayerSongSerializer,
    PlayerSongUpdateSerializer,
)
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.goal_song import remove_deleted_song_from_goal_song_selection
from apps.player.services.player_audio import clip_or_full_location
from apps.player.services.player_songs import (
    create_player_song,
    effective_song_audio_file,
    effective_song_status,
    retry_player_song_download,
    update_player_song_settings,
)

from .common import PLAYER_NOT_FOUND_DETAIL, SONG_NOT_FOUND_DETAIL, get_current_player


class PlayerSongClipAPIView(APIView):
    """Return and cache a short clip for a PlayerSong."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    @staticmethod
    def _parse_seconds_query(request: Request, key: str, default: int) -> int:
        raw = request.query_params.get(key)
        if not raw:
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    def get(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> HttpResponseRedirect:
        """Redirect to a short clip URL for the requested song."""
        start_seconds = max(0, self._parse_seconds_query(request, "start", 0))
        duration_seconds = self._parse_seconds_query(request, "duration", 8)
        duration_seconds = max(1, min(15, duration_seconds))

        song = (
            PlayerSong.objects
            .select_related("cached_song")
            .filter(id_uuid=song_id)
            .first()
        )
        if song is None:
            return HttpResponseRedirect("/")

        audio_file = effective_song_audio_file(song)
        if not audio_file:
            return HttpResponseRedirect("/")

        return HttpResponseRedirect(
            clip_or_full_location(
                audio_file=audio_file,
                song=song,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
            )
        )


class CurrentPlayerSongsAPIView(APIView):
    """List and create downloaded songs for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]
    parser_classes: ClassVar[list[type[object]]] = [
        JSONParser,
        FormParser,
        MultiPartParser,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the current player's downloaded songs."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        songs = (
            PlayerSong.objects
            .select_related("cached_song")
            .filter(player=player)
            .order_by("-created_at")
        )
        return Response(PlayerSongSerializer(songs, many=True).data)

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a new song download request for the current player."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerSongCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_audio = serializer.validated_data.get("audio_file")
        song, created = create_player_song(
            player=player,
            uploaded_audio=(
                uploaded_audio if isinstance(uploaded_audio, UploadedFile) else None
            ),
            spotify_url=str(serializer.validated_data.get("spotify_url") or ""),
        )
        return Response(
            PlayerSongSerializer(song).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CurrentPlayerSongDetailAPIView(APIView):
    """Update or delete a specific song for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def patch(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update per-song playback settings for a specific downloaded song."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        song = PlayerSong.objects.filter(player=player, id_uuid=song_id).first()
        if song is None:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerSongUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        update_player_song_settings(
            song=song,
            start_time_seconds=serializer.validated_data.get("start_time_seconds"),
            playback_speed=serializer.validated_data.get("playback_speed"),
        )
        return Response(PlayerSongSerializer(song).data)

    def delete(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Delete a specific downloaded song."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        song = PlayerSong.objects.filter(player=player, id_uuid=song_id).first()
        if song is None:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            remove_deleted_song_from_goal_song_selection(
                player=player,
                deleted_song_id=str(song.id_uuid),
            )
            song.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentPlayerSongRetryAPIView(APIView):
    """Retry downloading a failed song for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def post(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Reset song status and re-enqueue its download task."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        song = (
            PlayerSong.objects
            .select_related("cached_song")
            .filter(
                player=player,
                id_uuid=song_id,
            )
            .first()
        )
        if song is None:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        if effective_song_status(song) == PlayerSongStatus.READY:
            return Response(
                {"detail": "Song is already ready"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        retry_player_song_download(song)
        return Response(PlayerSongSerializer(song).data)
