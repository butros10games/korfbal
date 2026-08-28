"""Player profile and team-related API views."""

from __future__ import annotations

from typing import Any, cast

from rest_framework import mixins, permissions, status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.api.permissions import CanModifyPlayer
from apps.player.api.serializers import (
    PlayerPrivacySettingsSerializer,
    PlayerSerializer,
)
from apps.player.models.player import Player
from apps.player.services.player_queries import player_by_id, player_detail_queryset
from apps.player.services.player_settings import (
    delete_player_profile,
    player_privacy_settings,
    update_player_privacy_settings,
    update_player_profile,
)
from apps.player.services.player_teams import (
    followed_teams_for_player,
    grouped_teams_for_player,
)
from apps.team.api.serializers import TeamSerializer

from .common import (
    PLAYER_NOT_FOUND_DETAIL,
    PRIVATE_ACCOUNT_DETAIL,
    cache_viewer_player,
    get_current_player,
    get_viewer_player,
    player_serializer_context,
    resolve_player_access,
)


class PlayerViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Read, update, and delete player profiles."""

    queryset = player_detail_queryset()
    serializer_class = PlayerSerializer
    permission_classes = (
        permissions.IsAuthenticatedOrReadOnly,
        CanModifyPlayer,
    )
    lookup_field = "id_uuid"

    def get_object(self) -> Player:
        """Resolve the target and reuse it when it is also the viewer."""
        player = cast(Player, super().get_object())
        cache_viewer_player(self.request, player)
        return player

    def get_serializer_context(self) -> dict[str, object]:
        """Provide the serializer with an already-resolved viewer."""
        context: dict[str, object] = dict(super().get_serializer_context())
        context["viewer_player"] = get_viewer_player(self.request)
        return context

    def perform_update(self, serializer: Any) -> None:
        """Apply validated profile changes through the command service."""
        player = cast(Player, serializer.instance)
        update_player_profile(
            player=player,
            changes=cast(dict[str, object], serializer.validated_data),
        )
        refreshed = player_by_id(str(player.id_uuid))
        if refreshed is not None:
            serializer.instance = refreshed

    def perform_destroy(self, instance: Player) -> None:
        """Delete a profile through the command service."""
        delete_player_profile(instance)


class CurrentPlayerAPIView(KorfbalAPIView):
    """Return the profile for the active player (or a debug fallback)."""

    permission_classes = (permissions.AllowAny,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return the current player's profile."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        return Response(
            PlayerSerializer(
                player,
                context=player_serializer_context(request, current_player=player),
            ).data
        )


class PlayerFollowedTeamsAPIView(KorfbalAPIView):
    """Return teams followed by a player."""

    permission_classes = (permissions.AllowAny,)

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return teams followed by the requested player."""
        access = resolve_player_access(
            request,
            player_id=player_id,
            visibility_field="teams_visibility",
        )
        player = access.player
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        if access.forbidden:
            return Response(
                PRIVATE_ACCOUNT_DETAIL,
                status=status.HTTP_403_FORBIDDEN,
            )

        teams_qs = followed_teams_for_player(player)
        return Response(
            TeamSerializer(teams_qs, many=True, context={"request": request}).data
        )


class PlayerTeamsAPIView(KorfbalAPIView):
    """Return teams for a player grouped into playing/coaching/following."""

    permission_classes = (permissions.AllowAny,)

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return teams grouped as playing/coaching/following for a player."""
        access = resolve_player_access(
            request,
            player_id=player_id,
            visibility_field="teams_visibility",
        )
        player = access.player
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)
        if access.forbidden:
            return Response(
                PRIVATE_ACCOUNT_DETAIL,
                status=status.HTTP_403_FORBIDDEN,
            )

        team_groups = grouped_teams_for_player(player)

        return Response({
            "playing": TeamSerializer(
                team_groups.playing,
                many=True,
                context={"request": request},
            ).data,
            "coaching": TeamSerializer(
                team_groups.coaching,
                many=True,
                context={"request": request},
            ).data,
            "following": TeamSerializer(
                team_groups.following,
                many=True,
                context={"request": request},
            ).data,
        })


class CurrentPlayerTeamsAPIView(PlayerTeamsAPIView):
    """Return teams grouped for the current player."""

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return teams grouped as playing/coaching/following for the current player."""
        return super().get(request, None, *args, **kwargs)


class CurrentPlayerFollowedTeamsAPIView(PlayerFollowedTeamsAPIView):
    """Return teams followed by the current player."""

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return teams followed by the current player."""
        return super().get(request, None, *args, **kwargs)


class CurrentPlayerPrivacySettingsAPIView(KorfbalAPIView):
    """Read/update privacy visibility settings for the authenticated player."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return the authenticated player's privacy visibility settings."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        return Response(player_privacy_settings(player))

    def patch(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update the authenticated player's privacy visibility settings."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerPrivacySettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        update_player_privacy_settings(
            player=player,
            changes=serializer.validated_data,
        )

        return Response(
            PlayerSerializer(
                player,
                context=player_serializer_context(request, current_player=player),
            ).data
        )
