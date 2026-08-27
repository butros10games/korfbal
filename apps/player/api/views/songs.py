"""Song download and clip API views."""

from __future__ import annotations

import logging
from typing import Any

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import FileResponse, HttpResponseRedirect
from kombu.exceptions import OperationalError as KombuOperationalError
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.api.serializers import (
    PlayerSongCreateSerializer,
    PlayerSongSerializer,
    PlayerSongUpdateSerializer,
)
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.goal_song import remove_deleted_song_from_goal_song_selection
from apps.player.services.player_audio import ensure_goal_song_clip
from apps.player.services.player_songs import (
    create_player_song,
    effective_song_audio_file,
    effective_song_status,
    enqueue_download_for_player_song,
    retry_player_song_download,
    update_player_song_settings,
)

from .common import PLAYER_NOT_FOUND_DETAIL, SONG_NOT_FOUND_DETAIL, get_current_player


logger = logging.getLogger(__name__)


class PlayerSongClipAPIView(KorfbalAPIView):
    """Return and cache a short clip for a PlayerSong."""

    permission_classes = (permissions.AllowAny,)

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
        *args: Any,
        **kwargs: Any,
    ) -> FileResponse | HttpResponseRedirect | Response:
        """Stream a stable, cacheable short clip for the requested song."""
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

        clip_key = ensure_goal_song_clip(
            audio_file=audio_file,
            song=song,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
        )
        if request.query_params.get("stream") != "1":
            location = (
                default_storage.url(clip_key) if clip_key else str(audio_file.url)
            )
            return HttpResponseRedirect(location)

        if not clip_key:
            try:
                enqueue_download_for_player_song(song)
            except KombuOperationalError:
                logger.warning(
                    "Celery broker unavailable; could not prepare PlayerSong %s",
                    song.id_uuid,
                    exc_info=True,
                )
            response = Response(
                {"detail": "Goal sound clip is not prepared."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
            response["Retry-After"] = "2"
            return response

        stream = default_storage.open(clip_key, "rb")
        filename = clip_key.rsplit("/", maxsplit=1)[-1]

        response = FileResponse(
            stream,
            as_attachment=False,
            filename=filename,
            content_type="audio/mpeg",
        )
        # The versioned manifest URL changes whenever the song source/settings
        # change, so clients may safely retain these bytes for offline playback.
        response["Cache-Control"] = "private, max-age=31536000, immutable"
        response["X-Goal-Audio-Prepared"] = "1"
        return response


class CurrentPlayerSongsAPIView(KorfbalAPIView):
    """List and create downloaded songs for the authenticated player."""

    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = (
        JSONParser,
        FormParser,
        MultiPartParser,
    )

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
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
        *args: Any,
        **kwargs: Any,
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


class CurrentPlayerSongDetailAPIView(KorfbalAPIView):
    """Update or delete a specific song for the authenticated player."""

    permission_classes = (permissions.IsAuthenticated,)

    def patch(
        self,
        request: Request,
        song_id: str,
        *args: Any,
        **kwargs: Any,
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
        *args: Any,
        **kwargs: Any,
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


class CurrentPlayerSongRetryAPIView(KorfbalAPIView):
    """Retry downloading a failed song for the authenticated player."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(
        self,
        request: Request,
        song_id: str,
        *args: Any,
        **kwargs: Any,
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
