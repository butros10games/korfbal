"""Match event reads HTTP actions."""

from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    GoalType,
    MatchPart,
)
from apps.game_tracker.services.timeline_reads import (
    MATCH_TIMELINE_IDENTITY_VERSION,
    read_match_event_history,
    read_match_events,
    read_match_shots,
)
from apps.schedule.models import Match

from .constants import MATCH_TRACKER_DATA_NOT_FOUND
from .match_viewset_contracts import MatchViewSetContext
from .permissions import IsCoachOrAdmin


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


class MatchEventReadActionsMixin:
    """Provide the match event reads actions."""

    @action(detail=True, methods=("GET",), url_path="events")
    def events(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
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
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
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
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
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
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
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
        url_path="events/options",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_options(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
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
                    "username": player.display_name,
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
