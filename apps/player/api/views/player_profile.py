"""Player profile and team-related API views."""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.player.api.serializers import (
    PlayerPrivacySettingsSerializer,
    PlayerSerializer,
)
from apps.player.models.player import Player
from apps.player.privacy import can_view_by_visibility
from apps.player.services.player_overview import current_season
from apps.team.api.serializers import TeamSerializer
from apps.team.models import TeamData
from apps.team.models.team import Team

from .common import (
    AUTHENTICATION_REQUIRED_MESSAGE,
    PLAYER_NOT_FOUND_DETAIL,
    PRIVATE_ACCOUNT_DETAIL,
    get_current_player,
    get_viewer_player,
    player_detail_queryset,
)


class PlayerViewSet(viewsets.ModelViewSet):
    """Provide CRUD operations for players."""

    queryset = player_detail_queryset()
    serializer_class = PlayerSerializer
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticatedOrReadOnly,
    ]
    lookup_field = "id_uuid"

    def _ensure_can_modify(self, player: Player) -> None:
        user = self.request.user
        if not user or not getattr(user, "is_authenticated", False):
            raise PermissionDenied(AUTHENTICATION_REQUIRED_MESSAGE)

        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return

        user_id = getattr(user, "id", None)
        if user_id is None or player.user.id != user_id:
            raise PermissionDenied("You do not have permission to modify this player")

    def perform_update(self, serializer: Any) -> None:  # noqa: ANN401
        """Update a player after enforcing ownership/staff checks."""
        player = self.get_object()
        self._ensure_can_modify(player)
        serializer.save()

    def perform_destroy(self, instance: Player) -> None:
        """Delete a player after enforcing ownership/staff checks."""
        self._ensure_can_modify(instance)
        instance.delete()


class CurrentPlayerAPIView(APIView):
    """Return the profile for the active player (or a debug fallback)."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the current player's profile."""
        player = get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        return Response(PlayerSerializer(player, context={"request": request}).data)


class PlayerFollowedTeamsAPIView(APIView):
    """Return teams followed by a player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return teams followed by the requested player."""
        player = self._resolve_player(request, player_id)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        if player_id and not can_view_by_visibility(
            visibility=player.teams_visibility,
            viewer=get_viewer_player(request),
            target=player,
        ):
            return Response(
                PRIVATE_ACCOUNT_DETAIL,
                status=status.HTTP_403_FORBIDDEN,
            )

        teams_qs = (
            player.team_follow
            .all()
            .select_related("club")
            .order_by("club__name", "name")
        )
        return Response(
            TeamSerializer(teams_qs, many=True, context={"request": request}).data
        )

    @staticmethod
    def _resolve_player(request: Request, player_id: str | None) -> Player | None:
        if player_id:
            return player_detail_queryset().filter(id_uuid=player_id).first()
        return get_current_player(request)


class PlayerTeamsAPIView(APIView):
    """Return teams for a player grouped into playing/coaching/following."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return teams grouped as playing/coaching/following for a player."""
        player = self._resolve_player(request, player_id)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        if player_id and not can_view_by_visibility(
            visibility=player.teams_visibility,
            viewer=get_viewer_player(request),
            target=player,
        ):
            return Response(
                PRIVATE_ACCOUNT_DETAIL,
                status=status.HTTP_403_FORBIDDEN,
            )

        season = current_season()
        playing_qs = Team.objects.none()
        coaching_qs = Team.objects.none()
        if season is not None:
            playing_ids = (
                TeamData.objects
                .filter(season=season)
                .filter(players=player)
                .values_list("team_id", flat=True)
                .distinct()
            )
            coaching_ids = (
                TeamData.objects
                .filter(season=season)
                .filter(coach=player)
                .values_list("team_id", flat=True)
                .distinct()
            )

            playing_qs = (
                Team.objects
                .filter(id_uuid__in=playing_ids)
                .select_related("club")
                .order_by("club__name", "name")
            )
            coaching_qs = (
                Team.objects
                .filter(id_uuid__in=coaching_ids)
                .select_related("club")
                .order_by("club__name", "name")
            )

        following_qs = (
            player.team_follow
            .all()
            .select_related("club")
            .order_by("club__name", "name")
        )

        return Response({
            "playing": TeamSerializer(
                playing_qs,
                many=True,
                context={"request": request},
            ).data,
            "coaching": TeamSerializer(
                coaching_qs,
                many=True,
                context={"request": request},
            ).data,
            "following": TeamSerializer(
                following_qs,
                many=True,
                context={"request": request},
            ).data,
        })

    @staticmethod
    def _resolve_player(request: Request, player_id: str | None) -> Player | None:
        if player_id:
            return player_detail_queryset().filter(id_uuid=player_id).first()
        return get_current_player(request)


class CurrentPlayerTeamsAPIView(PlayerTeamsAPIView):
    """Return teams grouped for the current player."""

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return teams grouped as playing/coaching/following for the current player."""
        return super().get(request, None, *args, **kwargs)


class CurrentPlayerFollowedTeamsAPIView(PlayerFollowedTeamsAPIView):
    """Return teams followed by the current player."""

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return teams followed by the current player."""
        return super().get(request, None, *args, **kwargs)


class CurrentPlayerPrivacySettingsAPIView(APIView):
    """Read/update privacy visibility settings for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the authenticated player's privacy visibility settings."""
        player = player_detail_queryset().filter(user=request.user).first()
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        profile_visibility = player.profile_picture_visibility
        if profile_visibility == Player.Visibility.PRIVATE:
            profile_visibility = Player.Visibility.CLUB

        stats_visibility = player.stats_visibility
        if stats_visibility == Player.Visibility.PRIVATE:
            stats_visibility = Player.Visibility.CLUB

        teams_visibility = player.teams_visibility
        if teams_visibility == Player.Visibility.PRIVATE:
            teams_visibility = Player.Visibility.CLUB

        return Response({
            "profile_picture_visibility": profile_visibility,
            "stats_visibility": stats_visibility,
            "teams_visibility": teams_visibility,
        })

    def patch(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update the authenticated player's privacy visibility settings."""
        player = player_detail_queryset().filter(user=request.user).first()
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerPrivacySettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        update_fields: list[str] = []
        if "profile_picture_visibility" in serializer.validated_data:
            player.profile_picture_visibility = str(
                serializer.validated_data["profile_picture_visibility"]
            )
            update_fields.append("profile_picture_visibility")

        if "stats_visibility" in serializer.validated_data:
            player.stats_visibility = str(serializer.validated_data["stats_visibility"])
            update_fields.append("stats_visibility")

        if "teams_visibility" in serializer.validated_data:
            player.teams_visibility = str(serializer.validated_data["teams_visibility"])
            update_fields.append("teams_visibility")

        if update_fields:
            player.save(update_fields=update_fields)

        return Response(PlayerSerializer(player, context={"request": request}).data)
