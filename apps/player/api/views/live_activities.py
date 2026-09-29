"""iOS Live Activity registration endpoints for the current user."""

from __future__ import annotations

from typing import cast

from rest_framework import permissions, status
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.api.serializers import (
    MatchLiveActivityEndSerializer,
    MatchLiveActivityRegisterSerializer,
)
from apps.player.services.live_activities import (
    LiveActivityNotFoundError,
    end_live_activity,
    register_live_activity,
)
from apps.schedule.models.match import Match


class CurrentPlayerLiveActivitiesAPIView(KorfbalAPIView):
    """Register or end the Live Activity push token of this device."""

    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = (JSONParser,)

    def post(self, request: Request) -> Response:
        """Store the activity token so tracker changes reach the Lock Screen."""
        serializer = MatchLiveActivityRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match_id = str(serializer.validated_data["match_id"])
        if not Match.objects.filter(pk=match_id).exists():
            return Response(
                {"detail": "Match not found."}, status=status.HTTP_404_NOT_FOUND
            )
        activity, created = register_live_activity(
            user_id=cast(int, request.user.pk),
            match_id=match_id,
            push_token=serializer.validated_data["push_token"],
        )
        return Response(
            {"created": created, "id_uuid": str(activity.id_uuid)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request: Request) -> Response:
        """Stop pushing to an activity the phone dismissed or ended."""
        serializer = MatchLiveActivityEndSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            end_live_activity(
                user_id=cast(int, request.user.pk),
                push_token=serializer.validated_data["push_token"],
            )
        except LiveActivityNotFoundError:
            return Response(
                {"detail": "Live activity not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
