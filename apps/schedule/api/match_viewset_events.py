"""MatchViewSet mixin: event timeline + event editor endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.composition import apply_event_editor_command, change_publisher
from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services.event_editor import (
    DeleteGoalEvent,
    DeletePauseEvent,
    DeleteSubstitutionEvent,
    DeleteTimeoutEvent,
    EventEditorCommand,
    EventEditorResult,
    EventEditorValidationError,
)
from apps.game_tracker.services.event_reconciliation import (
    EventReconciliationError,
    ReconciliationResolution,
    pending_reconciliations,
    resolve_reconciliation,
)
from apps.game_tracker.services.match_timeline_payload import (
    serialize_goal_event,
    serialize_pause_event,
    serialize_substitute_event,
)
from apps.game_tracker.services.timeline_reads import (
    MATCH_TIMELINE_IDENTITY_VERSION,
    read_match_event_history,
    read_match_events,
    read_match_shots,
)
from apps.schedule.models import Match

from .constants import MATCH_TRACKER_DATA_NOT_FOUND
from .permissions import IsCoachOrAdmin
from .serializers import (
    PauseWriteSerializer,
    PlayerChangeWriteSerializer,
    ShotWriteSerializer,
    TimeoutWriteSerializer,
)


RECONCILIATION_REASON_MAX_LENGTH = 255


class _MatchViewSetLike(Protocol):
    def get_object(self) -> Match: ...

    def _match_data(self, match: Match) -> MatchData | None: ...


def _request_payload(request: Request) -> Mapping[str, Any]:
    """Return an object-shaped request body.

    Raises:
        ParseError: If the request body is not a JSON object.

    """
    payload = request.data
    if not isinstance(payload, Mapping):
        raise ParseError("Request body must be a JSON object.")
    return payload


def _apply_command(
    *,
    match_data: MatchData,
    request: Request,
    command: EventEditorCommand,
) -> EventEditorResult:
    try:
        return apply_event_editor_command(
            match_data_id=match_data.pk,
            actor=request.user,
            command=command,
        )
    except EventEditorValidationError as exc:
        raise ValidationError(exc.errors) from exc


def _parse_since_revision(request: Request) -> int | None:
    """Parse an optional non-negative timeline revision.

    Raises:
        ValueError: The supplied revision is not a non-negative integer.

    """
    raw_revision = request.query_params.get("since_revision")
    if raw_revision is None:
        return None

    try:
        revision = int(raw_revision)
    except ValueError as error:
        raise ValueError("Invalid 'since_revision'.") from error

    if revision < 0:
        raise ValueError("Invalid 'since_revision'.")
    return revision


class MatchEventsActionsMixin:
    """Adds match event timeline + event editor actions to `MatchViewSet`."""

    @action(detail=True, methods=("GET",), url_path="events")
    def events(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return match tracker events for a single match.

        This powers the korfbal-web Match page "Events" tab.

        Returns:
            Response: JSON payload with an ordered events list.

        """
        match: Match = self.get_object()
        match_data = self._match_data(match)
        identity_version_raw = request.query_params.get("identity_version")
        try:
            since_revision = _parse_since_revision(request)
        except ValueError as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if match_data is None:
            return Response(
                {
                    "mode": "full",
                    "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
                    "live_revision": 0,
                    "home_team_id": str(match.home_team.id_uuid),
                    "match_parts": [],
                    "events": [],
                    "status": "unknown",
                },
                status=status.HTTP_200_OK,
            )
        snapshot = read_match_events(
            match_data_id=match_data.pk,
            since_revision=since_revision,
            current_identity=(
                identity_version_raw == str(MATCH_TIMELINE_IDENTITY_VERSION)
            ),
        )
        return Response(snapshot.to_payload(), status=status.HTTP_200_OK)

    @action(detail=True, methods=("GET",), url_path="shots")
    def shots(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return shot attempts (scored + missed) for a single match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        identity_version_raw = request.query_params.get("identity_version")
        try:
            since_revision = _parse_since_revision(request)
        except ValueError as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if match_data is None:
            return Response(
                {
                    "mode": "full",
                    "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
                    "live_revision": 0,
                    "home_team_id": str(match.home_team.id_uuid),
                    "away_team_id": str(match.away_team.id_uuid),
                    "shots": [],
                    "status": "unknown",
                },
                status=status.HTTP_200_OK,
            )
        snapshot = read_match_shots(
            match_data_id=match_data.pk,
            since_revision=since_revision,
            current_identity=(
                identity_version_raw == str(MATCH_TIMELINE_IDENTITY_VERSION)
            ),
        )
        return Response(snapshot.to_payload(), status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/can-edit",
        permission_classes=[permissions.AllowAny],
    )
    def can_edit_events(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return whether the current user can edit match events."""
        return Response({"can_edit": IsCoachOrAdmin().has_permission(request, self)})

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/history",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_history(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return the complete append-only audit stream for authorized editors."""
        del request, args, kwargs
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if match_data is None:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )
        snapshot = read_match_event_history(match_data_id=match_data.pk)
        return Response(snapshot.to_payload(), status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/reconciliations",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_reconciliations(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return ambiguous cross-team reports requiring a decision."""
        del request, args, kwargs
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if match_data is None:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"reconciliations": pending_reconciliations(match_data)})

    @action(
        detail=True,
        methods=("POST",),
        url_path=r"events/reconciliations/(?P<reconciliation_id>[^/.]+)/resolve",
        permission_classes=[IsCoachOrAdmin],
    )
    def resolve_event_reconciliation(
        self: _MatchViewSetLike,
        request: Request,
        reconciliation_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Merge a duplicate pair or confirm that both events are real."""
        del args, kwargs
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if match_data is None:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )
        payload = _request_payload(request)
        decision = payload.get("decision")
        canonical_event_id = payload.get("canonical_event_id")
        reason = payload.get("reason", "")
        if not isinstance(decision, str) or not isinstance(reason, str):
            return Response(
                {"detail": "Invalid reconciliation decision."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if canonical_event_id is not None and not isinstance(canonical_event_id, str):
            return Response(
                {"detail": "Invalid canonical_event_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(reason) > RECONCILIATION_REASON_MAX_LENGTH:
            return Response(
                {"detail": "Reason must contain at most 255 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            resolved = resolve_reconciliation(
                ReconciliationResolution(
                    match_data=match_data,
                    reconciliation_id=reconciliation_id,
                    decision=decision,
                    canonical_event_id=canonical_event_id,
                    actor=request.user,
                    reason=reason,
                ),
                publisher=change_publisher,
            )
        except EventReconciliationError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({
            "id_uuid": str(resolved.pk),
            "decision": resolved.decision,
            "canonical_event_id": (
                str(resolved.canonical_event_id)
                if resolved.canonical_event_id
                else None
            ),
            "resolution_event_id": str(resolved.resolution_event_id),
        })

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/goals",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_goal(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
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
        result = _apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(),
        )
        shot = cast(Shot, result.event)
        return Response(
            serialize_goal_event(result.match_data, shot),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/goals/(?P<shot_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def goal_detail(
        self: _MatchViewSetLike,
        request: Request,
        shot_id: str,
        *args: Any,
        **kwargs: Any,
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
            result = _apply_command(
                match_data=match_data,
                request=request,
                command=DeleteGoalEvent(event_id=shot_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Goal event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = ShotWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = _apply_command(
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
            serialize_goal_event(result.match_data, shot),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/substitutes",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_substitute(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
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
        result = _apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(),
        )
        change = cast(PlayerChange, result.event)
        return Response(
            serialize_substitute_event(result.match_data, change),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/substitutes/(?P<change_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def substitute_detail(
        self: _MatchViewSetLike,
        request: Request,
        change_id: str,
        *args: Any,
        **kwargs: Any,
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
            result = _apply_command(
                match_data=match_data,
                request=request,
                command=DeleteSubstitutionEvent(event_id=change_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Substitution event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = PlayerChangeWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = _apply_command(
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
            serialize_substitute_event(result.match_data, change),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/pauses",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_pause(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
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
        result = _apply_command(
            match_data=match_data,
            request=request,
            command=serializer.to_command(),
        )
        pause = cast(Pause, result.event)
        return Response(
            serialize_pause_event(result.match_data, pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/pauses/(?P<pause_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def pause_detail(
        self: _MatchViewSetLike,
        request: Request,
        pause_id: str,
        *args: Any,
        **kwargs: Any,
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
            result = _apply_command(
                match_data=match_data,
                request=request,
                command=DeletePauseEvent(event_id=pause_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Pause event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = PauseWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = _apply_command(
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
            serialize_pause_event(result.match_data, pause),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/timeouts",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_timeout(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
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
        result = _apply_command(
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
            serialize_pause_event(result.match_data, timeout.pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/options",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_options(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return option lists needed to create/update match tracker events."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        match_parts = list(
            MatchPart.objects.filter(match_data=match_data).order_by("part_number")
        )
        player_groups = list(
            match_data.player_groups.select_related(
                "team",
                "starting_type",
                "current_type",
            ).prefetch_related(
                "players__user",
            )
        )
        goal_types = list(GoalType.objects.order_by("name"))

        players_by_id: dict[str, dict[str, str]] = {}
        for group in player_groups:
            for player in group.players.all():
                players_by_id[str(player.id_uuid)] = {
                    "id_uuid": str(player.id_uuid),
                    "username": player.user.username,
                }

        home_label = f"{match.home_team.club.name} {match.home_team.name}".strip()
        away_label = f"{match.away_team.club.name} {match.away_team.name}".strip()

        return Response(
            {
                "teams": [
                    {
                        "id_uuid": str(match.home_team.id_uuid),
                        "label": home_label,
                        "side": "home",
                    },
                    {
                        "id_uuid": str(match.away_team.id_uuid),
                        "label": away_label,
                        "side": "away",
                    },
                ],
                "match_parts": [
                    {
                        "id_uuid": str(part.id_uuid),
                        "part_number": part.part_number,
                        "start_time": part.start_time.isoformat(),
                        "end_time": (
                            part.end_time.isoformat() if part.end_time else None
                        ),
                        "active": part.active,
                    }
                    for part in match_parts
                ],
                "goal_types": [
                    {"id_uuid": str(goal_type.id_uuid), "name": goal_type.name}
                    for goal_type in goal_types
                ],
                "players": sorted(
                    players_by_id.values(),
                    key=lambda row: row["username"].lower(),
                ),
                "player_groups": [
                    {
                        "id_uuid": str(group.id_uuid),
                        "team_id": str(group.team_id),
                        "starting_type": group.starting_type.name,
                        "current_type": group.current_type.name,
                        "label": f"{group.team.name} - {group.starting_type.name}",
                    }
                    for group in player_groups
                ],
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/timeouts/(?P<timeout_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def timeout_detail(
        self: _MatchViewSetLike,
        request: Request,
        timeout_id: str,
        *args: Any,
        **kwargs: Any,
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
            result = _apply_command(
                match_data=match_data,
                request=request,
                command=DeleteTimeoutEvent(event_id=timeout_id),
            )
            if not result.found:
                return Response(
                    {"detail": "Timeout event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = TimeoutWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = _apply_command(
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
            serialize_pause_event(result.match_data, timeout.pause),
            status=status.HTTP_200_OK,
        )
