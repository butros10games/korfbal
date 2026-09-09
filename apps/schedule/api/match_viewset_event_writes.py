"""Match event writes HTTP actions."""

from __future__ import annotations

from typing import cast

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services.event_editor import (
    DeleteGoalEvent,
    DeletePauseEvent,
    DeletePossessionChangeEvent,
    DeleteSubstitutionEvent,
    DeleteTimeoutEvent,
)
from apps.game_tracker.services.match_timeline_payload import (
    serialize_goal_event,
    serialize_pause_event,
    serialize_substitute_event,
)
from apps.schedule.models import Match

from .constants import MATCH_TRACKER_DATA_NOT_FOUND
from .event_editor_commands import apply_command, mutation_payload
from .match_viewset_contracts import MatchViewSetContext
from .permissions import IsCoachOrAdmin
from .serializers import (
    PauseWriteSerializer,
    PlayerChangeWriteSerializer,
    ShotWriteSerializer,
    TimeoutWriteSerializer,
)


class MatchEventWriteActionsMixin:
    """Provide the match event writes actions."""

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/goals",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_goal(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Create a goal (Shot) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ShotWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(),
        )
        shot = cast(Shot, result.event)
        return Response(
            mutation_payload(
                result,
                serialize_goal_event(result.match_data, shot),
            ),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/goals/(?P<shot_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def goal_detail(
        self: MatchViewSetContext,
        request: Request,
        shot_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Update or delete an existing goal (Shot) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            result = apply_command(
                match_data=match_data,
                request=request,
                command=DeleteGoalEvent(event_id=shot_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Goal event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(
                mutation_payload(result, None),
                status=status.HTTP_200_OK,
            )

        serializer = ShotWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(event_id=shot_id),
        )
        if not result.found:
            return Response(
                {"detail": "Goal event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        shot = cast(Shot, result.event)
        return Response(
            mutation_payload(
                result,
                serialize_goal_event(result.match_data, shot),
            ),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("DELETE",),
        url_path=r"events/possession-changes/(?P<event_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def possession_change_detail(
        self: MatchViewSetContext,
        request: Request,
        event_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Delete a possession-change event from this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        result = apply_command(
            match_data=match_data,
            request=request,
            command=DeletePossessionChangeEvent(event_id=event_id),
        )
        if not result.found:
            return Response(
                {"detail": "Possession-change event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            mutation_payload(result, None),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/substitutes",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_substitute(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Create a substitution (PlayerChange) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PlayerChangeWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(),
        )
        change = cast(PlayerChange, result.event)
        return Response(
            mutation_payload(
                result,
                serialize_substitute_event(result.match_data, change),
            ),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/substitutes/(?P<change_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def substitute_detail(
        self: MatchViewSetContext,
        request: Request,
        change_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Update or delete a substitution (PlayerChange) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            result = apply_command(
                match_data=match_data,
                request=request,
                command=DeleteSubstitutionEvent(event_id=change_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Substitution event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(
                mutation_payload(result, None),
                status=status.HTTP_200_OK,
            )

        serializer = PlayerChangeWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(event_id=change_id),
        )
        if not result.found:
            return Response(
                {"detail": "Substitution event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        change = cast(PlayerChange, result.event)
        return Response(
            mutation_payload(
                result,
                serialize_substitute_event(result.match_data, change),
            ),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/pauses",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_pause(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Create a pause (Pause) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PauseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(),
        )
        pause = cast(Pause, result.event)
        return Response(
            mutation_payload(
                result,
                serialize_pause_event(result.match_data, pause),
            ),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/pauses/(?P<pause_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def pause_detail(
        self: MatchViewSetContext,
        request: Request,
        pause_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Update or delete a pause (Pause) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            result = apply_command(
                match_data=match_data,
                request=request,
                command=DeletePauseEvent(event_id=pause_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Pause event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(
                mutation_payload(result, None),
                status=status.HTTP_200_OK,
            )

        serializer = PauseWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(event_id=pause_id),
        )
        if not result.found:
            return Response(
                {"detail": "Pause event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        pause = cast(Pause, result.event)
        return Response(
            mutation_payload(
                result,
                serialize_pause_event(result.match_data, pause),
            ),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/timeouts",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_timeout(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Create a timeout (Timeout + Pause) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = TimeoutWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(),
        )
        timeout = cast(Timeout, result.event)
        if not timeout.pause:
            return Response(
                {"detail": "Timeout was created without a pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            mutation_payload(
                result,
                serialize_pause_event(result.match_data, timeout.pause),
            ),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/timeouts/(?P<timeout_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def timeout_detail(
        self: MatchViewSetContext,
        request: Request,
        timeout_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Update or delete a timeout (Timeout + Pause) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            result = apply_command(
                match_data=match_data,
                request=request,
                command=DeleteTimeoutEvent(event_id=timeout_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Timeout event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(
                mutation_payload(result, None),
                status=status.HTTP_200_OK,
            )

        serializer = TimeoutWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(event_id=timeout_id),
        )
        if not result.found:
            return Response(
                {"detail": "Timeout event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        timeout = cast(Timeout, result.event)
        if not timeout.pause:
            return Response(
                {"detail": "Timeout has no pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            mutation_payload(
                result,
                serialize_pause_event(result.match_data, timeout.pause),
            ),
            status=status.HTTP_200_OK,
        )
