"""Match event writes HTTP actions."""

from __future__ import annotations

from typing import cast

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    MatchData,
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
    EventEditorResult,
)
from apps.game_tracker.services.match_timeline_payload import (
    serialize_goal_event,
    serialize_pause_event,
    serialize_substitute_event,
)

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


type EventWriteSerializer = (
    ShotWriteSerializer
    | PlayerChangeWriteSerializer
    | PauseWriteSerializer
    | TimeoutWriteSerializer
)
type DeleteEventCommand = (
    DeleteGoalEvent
    | DeleteSubstitutionEvent
    | DeletePauseEvent
    | DeleteTimeoutEvent
    | DeletePossessionChangeEvent
)


def _require_match_data(view: MatchViewSetContext) -> MatchData:
    """Resolve tracker data after the viewset applies object permissions.

    Raises:
        NotFound: The match has no tracker data.

    """
    match_data = view._match_data(view.get_object())
    if match_data is None:
        raise NotFound(MATCH_TRACKER_DATA_NOT_FOUND)
    return match_data


def _write_event(
    match_data: MatchData,
    request: Request,
    serializer_class: type[EventWriteSerializer],
    *,
    event_id: str | None = None,
    not_found: str = "Event not found.",
) -> EventEditorResult:
    """Validate input and run one create or update through the command boundary.

    Raises:
        NotFound: The event to update does not exist in this match.

    """
    serializer = serializer_class(data=request.data, partial=event_id is not None)
    serializer.is_valid(raise_exception=True)
    result = apply_command(
        match_data=match_data,
        request=request,
        command=serializer.to_command(event_id=event_id),
    )
    if not result.found:
        raise NotFound(not_found)
    return result


def _delete_event(
    match_data: MatchData,
    request: Request,
    command: DeleteEventCommand,
    not_found: str,
) -> Response:
    """Delete through the command boundary and return its committed revision.

    Raises:
        NotFound: The event does not exist in this match.

    """
    result = apply_command(match_data=match_data, request=request, command=command)
    if not result.found:
        raise NotFound(not_found)
    return Response(mutation_payload(result, None), status=status.HTTP_200_OK)


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
        match_data = _require_match_data(self)

        result = _write_event(match_data, request, ShotWriteSerializer)
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
        match_data = _require_match_data(self)

        if request.method == "DELETE":
            return _delete_event(
                match_data,
                request,
                DeleteGoalEvent(event_id=shot_id),
                "Goal event not found.",
            )

        result = _write_event(
            match_data,
            request,
            ShotWriteSerializer,
            event_id=shot_id,
            not_found="Goal event not found.",
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
        match_data = _require_match_data(self)

        return _delete_event(
            match_data,
            request,
            DeletePossessionChangeEvent(event_id=event_id),
            "Possession-change event not found.",
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
        match_data = _require_match_data(self)

        result = _write_event(match_data, request, PlayerChangeWriteSerializer)
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
        match_data = _require_match_data(self)

        if request.method == "DELETE":
            return _delete_event(
                match_data,
                request,
                DeleteSubstitutionEvent(event_id=change_id),
                "Substitution event not found.",
            )

        result = _write_event(
            match_data,
            request,
            PlayerChangeWriteSerializer,
            event_id=change_id,
            not_found="Substitution event not found.",
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
        match_data = _require_match_data(self)

        result = _write_event(match_data, request, PauseWriteSerializer)
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
        match_data = _require_match_data(self)

        if request.method == "DELETE":
            return _delete_event(
                match_data,
                request,
                DeletePauseEvent(event_id=pause_id),
                "Pause event not found.",
            )

        result = _write_event(
            match_data,
            request,
            PauseWriteSerializer,
            event_id=pause_id,
            not_found="Pause event not found.",
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
        match_data = _require_match_data(self)

        result = _write_event(match_data, request, TimeoutWriteSerializer)
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
        match_data = _require_match_data(self)

        if request.method == "DELETE":
            return _delete_event(
                match_data,
                request,
                DeleteTimeoutEvent(event_id=timeout_id),
                "Timeout event not found.",
            )

        result = _write_event(
            match_data,
            request,
            TimeoutWriteSerializer,
            event_id=timeout_id,
            not_found="Timeout event not found.",
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
