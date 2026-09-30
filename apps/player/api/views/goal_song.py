"""Goal-song API views."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.api.serializers import PlayerSerializer
from apps.player.composition import apply_goal_song_settings
from apps.player.services.goal_song import (
    GoalSongPayloadError,
    GoalSongSelectionError,
    parse_goal_song_patch_payload,
)
from apps.player.services.player_queries import player_detail_queryset
from apps.player.services.player_songs import InvalidSongClipError

from .common import (
    PLAYER_NOT_FOUND_DETAIL,
    get_current_player,
    player_serializer_context,
)


class CurrentPlayerGoalSongAPIView(KorfbalAPIView):
    """Update goal song configuration for the current player."""

    permission_classes = (permissions.IsAuthenticated,)

    @staticmethod
    def _selection_error_response(exc: GoalSongSelectionError) -> Response:
        payload: dict[str, object] = {"detail": exc.detail}
        if exc.missing:
            payload["missing"] = exc.missing
        if exc.not_ready:
            payload["not_ready"] = exc.not_ready
        return Response(payload, status=status.HTTP_400_BAD_REQUEST)

    def patch(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update goal-song configuration for the authenticated player."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        if not isinstance(request.data, Mapping):
            return Response(
                {"detail": "Invalid payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            apply_goal_song_settings(
                player=player,
                settings=parse_goal_song_patch_payload(request.data),
            )
        except GoalSongPayloadError as exc:
            return Response({"detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except GoalSongSelectionError as exc:
            return self._selection_error_response(exc)
        except InvalidSongClipError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # The update wrote PlayerSong rows and the selection; the request's
        # prefetched profile still holds the previous clip offsets.
        updated = player_detail_queryset().get(pk=player.pk)
        return Response(
            PlayerSerializer(
                updated,
                context=player_serializer_context(request, current_player=updated),
            ).data
        )
