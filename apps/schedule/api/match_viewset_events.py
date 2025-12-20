"""MatchViewSet mixin: event timeline + event editor endpoints."""

from __future__ import annotations

from typing import Any, Protocol

from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.schedule.models import Match

from .constants import MATCH_TRACKER_DATA_NOT_FOUND
from .match_events_payload import (
    _build_match_events,
    _build_match_shots,
    _serialize_goal_event,
    _serialize_pause_event,
    _serialize_substitute_event,
)
from .permissions import IsCoachOrAdmin
from .serializers import (
    PauseWriteSerializer,
    PlayerChangeWriteSerializer,
    ShotWriteSerializer,
    TimeoutWriteSerializer,
)


class _MatchViewSetLike(Protocol):
    def get_object(self) -> Match: ...


class MatchEventsActionsMixin:
    """Adds match event timeline + event editor actions to `MatchViewSet`."""

    @action(detail=True, methods=["GET"], url_path="events")  # type: ignore[arg-type]
    def events(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return match tracker events for a single match.

        This powers the korfbal-web Match page "Events" tab.

        Returns:
            Response: JSON payload with an ordered events list.

        """
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data or match_data.status == "upcoming":
            return Response(
                {
                    "home_team_id": str(match.home_team.id_uuid),
                    "match_parts": [],
                    "events": [],
                    "status": match_data.status if match_data else "unknown",
                },
                status=status.HTTP_200_OK,
            )

        match_parts_payload = [
            {
                "id_uuid": str(part.id_uuid),
                "part_number": part.part_number,
                "start_time": part.start_time.isoformat() if part.start_time else None,
                "end_time": part.end_time.isoformat() if part.end_time else None,
                "active": bool(part.active),
            }
            for part in MatchPart.objects
            .filter(match_data=match_data)
            .order_by("part_number", "start_time")
            .all()
        ]

        events_payload = _build_match_events(match_data)
        return Response(
            {
                "home_team_id": str(match.home_team.id_uuid),
                "match_parts": match_parts_payload,
                "events": events_payload,
                "status": match_data.status,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["GET"], url_path="shots")  # type: ignore[arg-type]
    def shots(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return shot attempts (scored + missed) for a single match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data or match_data.status == "upcoming":
            return Response(
                {
                    "home_team_id": str(match.home_team.id_uuid),
                    "away_team_id": str(match.away_team.id_uuid),
                    "shots": [],
                    "status": match_data.status if match_data else "unknown",
                },
                status=status.HTTP_200_OK,
            )

        shots_payload = _build_match_shots(match_data)
        return Response(
            {
                "home_team_id": str(match.home_team.id_uuid),
                "away_team_id": str(match.away_team.id_uuid),
                "shots": shots_payload,
                "status": match_data.status,
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/can-edit",
        permission_classes=[permissions.AllowAny],
    )  # type: ignore[arg-type]
    def can_edit_events(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return whether the current user can edit match events."""
        return Response({"can_edit": IsCoachOrAdmin().has_permission(request, self)})

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/goals",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_goal(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a goal (Shot) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ShotWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        shot = serializer.save()
        return Response(
            _serialize_goal_event(match_data, shot),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/goals/(?P<shot_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def goal_detail(
        self: _MatchViewSetLike,
        request: Request,
        shot_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete an existing goal (Shot) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        shot = Shot.objects.filter(id_uuid=shot_id, match_data=match_data).first()
        if not shot:
            return Response(
                {"detail": "Goal event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            shot.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = ShotWriteSerializer(
            instance=shot,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        shot = serializer.save()
        return Response(
            _serialize_goal_event(match_data, shot),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/substitutes",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_substitute(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a substitution (PlayerChange) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PlayerChangeWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        change = serializer.save()
        return Response(
            _serialize_substitute_event(match_data, change),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/substitutes/(?P<change_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def substitute_detail(
        self: _MatchViewSetLike,
        request: Request,
        change_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete a substitution (PlayerChange) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        change = PlayerChange.objects.filter(
            id_uuid=change_id,
            player_group__match_data=match_data,
        ).first()
        if not change:
            return Response(
                {"detail": "Substitution event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            change.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = PlayerChangeWriteSerializer(
            instance=change,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        change = serializer.save()
        return Response(
            _serialize_substitute_event(match_data, change),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/pauses",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_pause(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a pause (Pause) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PauseWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        pause = serializer.save()
        return Response(
            _serialize_pause_event(match_data, pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/pauses/(?P<pause_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def pause_detail(
        self: _MatchViewSetLike,
        request: Request,
        pause_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete a pause (Pause) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        pause = Pause.objects.filter(id_uuid=pause_id, match_data=match_data).first()
        if not pause:
            return Response(
                {"detail": "Pause event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            Timeout.objects.filter(pause=pause).delete()
            pause.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = PauseWriteSerializer(
            instance=pause,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        pause = serializer.save()
        return Response(
            _serialize_pause_event(match_data, pause),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/timeouts",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_timeout(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a timeout (Timeout + Pause) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = TimeoutWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        timeout = serializer.save()
        if not timeout.pause:
            return Response(
                {"detail": "Timeout was created without a pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            _serialize_pause_event(match_data, timeout.pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/options",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def event_options(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return option lists needed to create/update match tracker events."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
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
    )  # type: ignore[arg-type]
    def timeout_detail(
        self: _MatchViewSetLike,
        request: Request,
        timeout_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete a timeout (Timeout + Pause) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        timeout = (
            Timeout.objects
            .select_related("pause")
            .filter(
                id_uuid=timeout_id,
                match_data=match_data,
            )
            .first()
        )
        if not timeout:
            return Response(
                {"detail": "Timeout event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        pause = timeout.pause
        if request.method == "DELETE":
            timeout.delete()
            if pause:
                pause.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = TimeoutWriteSerializer(
            instance=timeout,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        timeout = serializer.save()
        if not timeout.pause:
            return Response(
                {"detail": "Timeout has no pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            _serialize_pause_event(match_data, timeout.pause),
            status=status.HTTP_200_OK,
        )
