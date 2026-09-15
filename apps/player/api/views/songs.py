"""Song download and clip API views."""

from __future__ import annotations

import math
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.http import FileResponse, HttpResponseRedirect
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.api.serializers import (
    PlayerSongClipCreateSerializer,
    PlayerSongCreateSerializer,
    PlayerSongSerializer,
    PlayerSongUpdateSerializer,
)
from apps.player.composition import (
    audio_storage,
    create_player_song,
    create_song_clip,
    resolve_player_song_clip,
    retry_owned_player_song_download,
    update_owned_player_song_settings,
)
from apps.player.services.player_song_queries import (
    owned_player_song_or_none,
    player_songs_for_player,
)
from apps.player.services.player_songs import (
    MAX_SOURCE_SECONDS,
    InvalidSongClipError,
    PlayerSongAlreadyReadyError,
    PlayerSongClipRequest,
    PlayerSongNotFoundError,
    PlayerSongSettingsPatch,
    delete_owned_player_song,
)

from .common import PLAYER_NOT_FOUND_DETAIL, SONG_NOT_FOUND_DETAIL, get_current_player


class PlayerSongClipAPIView(KorfbalAPIView):
    """Return and cache a short clip for a PlayerSong."""

    permission_classes = (permissions.AllowAny,)

    @staticmethod
    def _parse_seconds_query(request: Request, key: str, default: int) -> int:
        raw = request.query_params.get(key)
        if not raw:
            return default
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(seconds):
            raise ValidationError({key: "Must be a finite number of seconds."})
        return int(seconds)

    def get(
        self,
        request: Request,
        song_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> FileResponse | HttpResponseRedirect | Response:
        """Stream a stable, cacheable short clip for the requested song.

        Raises:
            ValidationError: The requested start exceeds the supported source length.

        """
        start_seconds = max(0, self._parse_seconds_query(request, "start", 0))
        if start_seconds >= MAX_SOURCE_SECONDS:
            raise ValidationError({"start": "Must be less than 900 seconds."})
        duration_seconds = self._parse_seconds_query(request, "duration", 8)
        duration_seconds = max(1, min(15, duration_seconds))

        stream_requested = request.query_params.get("stream") == "1"
        clip = resolve_player_song_clip(
            request=PlayerSongClipRequest(
                song_id=song_id,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
                enqueue_if_missing=stream_requested,
            )
        )
        if clip is None:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        if not stream_requested:
            location = (
                audio_storage.url(clip.clip_key)
                if clip.clip_key
                else str(clip.audio_file.url)
            )
            return HttpResponseRedirect(location)

        if not clip.clip_key:
            response = Response(
                {"detail": "Goal sound clip is not prepared."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
            response["Retry-After"] = "2"
            return response

        try:
            stream = audio_storage.open(clip.clip_key)
        except FileNotFoundError:
            return Response(
                {"detail": "Goal sound clip file not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        filename = clip.clip_key.rsplit("/", maxsplit=1)[-1]

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

        songs = player_songs_for_player(player)
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
        creation = create_player_song(
            player=player,
            uploaded_audio=(
                uploaded_audio if isinstance(uploaded_audio, UploadedFile) else None
            ),
            spotify_url=str(serializer.validated_data.get("spotify_url") or ""),
            source_url=str(serializer.validated_data.get("source_url") or ""),
        )
        return Response(
            PlayerSongSerializer(creation.song).data,
            status=(
                status.HTTP_201_CREATED if creation.created else status.HTTP_200_OK
            ),
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

        if owned_player_song_or_none(player=player, song_id=song_id) is None:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerSongUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            song = update_owned_player_song_settings(
                player=player,
                song_id=song_id,
                settings=PlayerSongSettingsPatch(**serializer.validated_data),
            )
        except PlayerSongNotFoundError:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        except InvalidSongClipError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
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

        try:
            delete_owned_player_song(player=player, song_id=song_id)
        except PlayerSongNotFoundError:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
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

        try:
            song = retry_owned_player_song_download(
                player=player,
                song_id=song_id,
            )
        except PlayerSongNotFoundError:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        except PlayerSongAlreadyReadyError:
            return Response(
                {"detail": "Song is already ready", "code": "song_already_ready"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(PlayerSongSerializer(song).data)


class CurrentPlayerSongClipsAPIView(KorfbalAPIView):
    """Create independently selectable clips of the current player's audio."""

    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(
        request=PlayerSongClipCreateSerializer, responses={201: PlayerSongSerializer}
    )
    def post(
        self, request: Request, song_id: str, *args: Any, **kwargs: Any
    ) -> Response:
        """Create a clip only from an owned, ready source."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        serializer = PlayerSongClipCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            song = create_song_clip(
                owner=player,
                song_id=song_id,
                settings=PlayerSongSettingsPatch(**serializer.validated_data),
            )
        except PlayerSongNotFoundError:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        except InvalidSongClipError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PlayerSongSerializer(song).data, status=status.HTTP_201_CREATED)
