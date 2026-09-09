"""Tournament referee tracker API endpoints and helpers."""

from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.permissions import can_score_match
from apps.tournament.api.schema import DeleteBodySchema
from apps.tournament.api.serializers import (
    TournamentRefereeEventDeleteSerializer,
    TournamentRefereeGoalSerializer,
    TournamentRefereeReadySerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import TournamentMatch
from apps.tournament.services.referee_tracker import (
    RefereeTrackerError,
    build_referee_tracker_state,
    mark_field_ready,
    record_goal,
    remove_latest_goal,
    valid_guest_referee_claim,
)

from .common import lock_tournament_for_match


REFEREE_REVISION_CONFLICT_DETAIL = (
    "De wedstrijd is elders gewijzigd. De nieuwste stand wordt getoond."
)


def _user_display_name(user: object) -> str:
    full_name_getter = getattr(user, "get_full_name", None)
    full_name = full_name_getter().strip() if callable(full_name_getter) else ""
    return full_name or str(getattr(user, "username", "")) or str(user)


def _referee_match(
    request: Request, match_id: str, *, lock: bool
) -> tuple[TournamentMatch, object | None, str]:
    queryset = TournamentMatch.objects.select_related(
        "tournament",
        "field",
        "home_team",
        "away_team",
        "referee_team",
        "referee_player",
    )
    if lock:
        tournament = lock_tournament_for_match(match_id)
        queryset = queryset.select_for_update(of=("self",))
        queryset = queryset.filter(tournament=tournament)
    match = get_object_or_404(queryset, id_uuid=match_id)
    if can_score_match(request.user, match):
        return match, request.user, _user_display_name(request.user)
    if valid_guest_referee_claim(match, request.query_params.get("token")):
        return match, None, match.referee_name
    raise PermissionDenied(
        "Deze scheidsrechtertoegang is ongeldig of de wedstrijd is afgerond."
    )


def _referee_conflict(match: TournamentMatch, detail: str) -> Response:
    return Response(
        {
            "code": "referee_tracker_conflict",
            "detail": detail,
            "state": build_referee_tracker_state(match),
        },
        status=status.HTTP_409_CONFLICT,
    )


class TournamentRefereeTrackerView(APIView):
    """Return the focused state required by a field referee."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        responses={200: OpenApiTypes.OBJECT},
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY)],
    )
    def get(self, request: Request, match_id: str) -> Response:
        """Return a match only when the viewer may score its field."""
        match, _, _ = _referee_match(request, match_id, lock=False)
        return Response(build_referee_tracker_state(match))


class TournamentRefereeReadyView(APIView):
    """Record that one fixture's field is ready for the central start."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        request=TournamentRefereeReadySerializer,
        responses={200: OpenApiTypes.OBJECT},
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY)],
    )
    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Apply an idempotent, revision-checked readiness command."""
        serializer = TournamentRefereeReadySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match, actor, actor_name = _referee_match(request, match_id, lock=True)
        if match.field_ready_at is not None:
            return Response(build_referee_tracker_state(match))
        expected_revision = serializer.validated_data["expected_revision"]
        if expected_revision != match.revision:
            return _referee_conflict(
                match,
                REFEREE_REVISION_CONFLICT_DETAIL,
            )
        try:
            mark_field_ready(match, actor=actor, actor_name=actor_name)
        except RefereeTrackerError as exc:
            return _referee_conflict(match, str(exc))
        touch_tournament(match.tournament)
        return Response(build_referee_tracker_state(match))


class TournamentRefereeGoalView(APIView):
    """Record one home or away goal from the field referee."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        request=TournamentRefereeGoalSerializer,
        responses={200: OpenApiTypes.OBJECT},
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY)],
    )
    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Increment exactly one score under the aggregate lock."""
        serializer = TournamentRefereeGoalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match, actor, actor_name = _referee_match(request, match_id, lock=True)
        expected_revision = serializer.validated_data["expected_revision"]
        if expected_revision != match.revision:
            return _referee_conflict(
                match,
                REFEREE_REVISION_CONFLICT_DETAIL,
            )
        try:
            record_goal(
                match,
                side=serializer.validated_data["side"],
                actor=actor,
                actor_name=actor_name,
            )
        except RefereeTrackerError as exc:
            return _referee_conflict(match, str(exc))
        touch_tournament(match.tournament)
        return Response(build_referee_tracker_state(match))


class TournamentRefereeLatestEventView(APIView):
    """Remove the exact latest goal currently visible to a referee."""

    schema = DeleteBodySchema()

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        request=TournamentRefereeEventDeleteSerializer,
        responses={200: OpenApiTypes.OBJECT},
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY)],
    )
    @transaction.atomic
    def delete(self, request: Request, match_id: str) -> Response:
        """Undo one goal while preserving an append-only correction audit."""
        serializer = TournamentRefereeEventDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match, actor, actor_name = _referee_match(request, match_id, lock=True)
        if serializer.validated_data["expected_revision"] != match.revision:
            return _referee_conflict(match, REFEREE_REVISION_CONFLICT_DETAIL)
        try:
            remove_latest_goal(
                match,
                event_id=serializer.validated_data["event_id"],
                actor=actor,
                actor_name=actor_name,
            )
        except RefereeTrackerError as exc:
            return _referee_conflict(match, str(exc))
        touch_tournament(match.tournament)
        return Response(build_referee_tracker_state(match))
