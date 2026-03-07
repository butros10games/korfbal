"""Goal-song API views."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.player.api.serializers import PlayerSerializer
from apps.player.models.player import Player
from apps.player.services.goal_song import (
    GoalSongPayloadError,
    GoalSongSelectionError,
    apply_goal_song_song_ids,
    parse_goal_song_patch_payload,
)

from .common import PLAYER_NOT_FOUND_DETAIL


class CurrentPlayerGoalSongAPIView(APIView):
    """Update goal song configuration for the current player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    @staticmethod
    def _current_player(request: Request) -> Player | None:
        return Player.objects.select_related("user").filter(user=request.user).first()

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
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update goal-song configuration for the authenticated player."""
        player = self._current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        if not isinstance(request.data, Mapping):
            return Response(
                {"detail": "Invalid payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            parsed = parse_goal_song_patch_payload(request.data)
        except GoalSongPayloadError as exc:
            return Response(
                {"detail": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        update_fields: list[str] = []
        if parsed.goal_song_uri_provided:
            player.goal_song_uri = parsed.goal_song_uri or ""
            update_fields.append("goal_song_uri")
        if parsed.song_start_time_provided:
            player.song_start_time = parsed.song_start_time
            update_fields.append("song_start_time")

        if parsed.goal_song_ids_provided:
            try:
                update_fields.extend(
                    apply_goal_song_song_ids(
                        player=player,
                        ids=parsed.goal_song_song_ids or [],
                    )
                )
            except GoalSongSelectionError as exc:
                return self._selection_error_response(exc)

        if update_fields:
            player.save(update_fields=list(dict.fromkeys(update_fields)))

        return Response(PlayerSerializer(player, context={"request": request}).data)
