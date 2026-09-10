"""Cup setup, direct draws and revision-checked referee commands."""

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.composition import touch_tournament
from apps.tournament.services.cup_draw import generate_cup_draw
from apps.tournament.services.cup_planning import CupMatchPlan, plan_cup_match
from apps.tournament.services.cups import (
    CupError,
    apply_cup_command,
    start_cup_match,
    validate_cup_rules,
)
from apps.tournament.services.match_operations import TournamentMatchOperationError
from apps.tournament.services.referee_tracker import build_referee_tracker_state
from apps.tournament.services.snapshot import build_tournament_snapshot

from .common import Conflict, lock_tournament, require_manager
from .referee_tracker import _referee_conflict, _referee_match


class CupSetupSerializer(serializers.Serializer):
    """Require explicit scoring rules and the current event revision."""

    expected_revision = serializers.IntegerField(min_value=0)
    rules = serializers.JSONField()
    generate_draw = serializers.BooleanField(default=False)


class CupCommandSerializer(serializers.Serializer):
    """One auditable cup progression command."""

    expected_revision = serializers.IntegerField(min_value=0)
    command = serializers.ChoiceField(
        choices=["start", "advance", "penalty", "undo_penalty"]
    )
    side = serializers.ChoiceField(choices=["home", "away"], required=False)
    scored = serializers.BooleanField(required=False)


class TournamentCupSetupView(APIView):
    """Configure an unstarted cup and optionally generate its direct draw."""

    @extend_schema(request=CupSetupSerializer, responses={200: OpenApiTypes.OBJECT})
    @transaction.atomic
    def post(self, request: Request, tournament_id: str) -> Response:
        """Freeze rules before fixtures are created.

        Raises:
            Conflict: If another manager changed this event.
            ValidationError: If rules or the requested draw are invalid.
            CupError: Converted to an API validation error for invalid setup.

        """
        tournament = lock_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = CupSetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["expected_revision"] != tournament.live_revision:
            raise Conflict()
        try:
            rules = validate_cup_rules(data["rules"])
            if tournament.matches.exists() and tournament.cup_rules != rules:
                raise CupError(
                    "Bekerregels staan vast zodra wedstrijden zijn aangemaakt."
                )
            tournament.cup_rules = rules
            tournament.match_duration_minutes = sum(rules["regular_minutes"])
            tournament.save(
                update_fields=["cup_rules", "match_duration_minutes", "updated_at"]
            )
            if data["generate_draw"]:
                generate_cup_draw(tournament)
        except CupError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))


class TournamentCupCommandView(APIView):
    """Track phases and penalties with the existing referee authorization."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        request=CupCommandSerializer,
        responses={200: OpenApiTypes.OBJECT},
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY)],
    )
    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Apply an exact-revision command under the tournament-first lock."""
        serializer = CupCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        match, actor, actor_name = _referee_match(request, match_id, lock=True)
        if data["expected_revision"] != match.revision:
            return _referee_conflict(
                match, "De wedstrijd is elders gewijzigd. Controleer de nieuwste stand."
            )
        try:
            with transaction.atomic():
                if data["command"] == "start":
                    require_manager(request, match.tournament)
                    start_cup_match(match, actor)
                else:
                    apply_cup_command(
                        match,
                        payload=data,
                        actor=actor,
                        actor_name=actor_name,
                    )
        except (CupError, TournamentMatchOperationError) as exc:
            match.refresh_from_db()
            return _referee_conflict(match, str(exc))
        touch_tournament(match.tournament)
        return Response(build_referee_tracker_state(match))


class CupPlanSerializer(serializers.Serializer):
    """Explicit native round and bracket planning values."""

    expected_revision = serializers.IntegerField(min_value=0)
    round_name = serializers.CharField(max_length=120)
    round_number = serializers.IntegerField(min_value=1, max_value=128)
    starts_at = serializers.DateTimeField()
    field_id = serializers.UUIDField()
    next_match_id = serializers.UUIDField(allow_null=True)
    winner_to_side = serializers.ChoiceField(choices=["", "home", "away"])
    replace_destination_team = serializers.BooleanField(default=False)
    expected_destination_revision = serializers.IntegerField(
        min_value=0, required=False
    )


class TournamentCupPlanView(APIView):
    """Plan a cup fixture without requiring a fabricated pool."""

    @extend_schema(request=CupPlanSerializer, responses={200: OpenApiTypes.OBJECT})
    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Apply manager-owned planning under the tournament-first lock.

        Raises:
            Conflict: If the fixture changed since opening its plan.
            ValidationError: If the bracket or field would be inconsistent.

        """
        match, _, _ = _referee_match(request, match_id, lock=True)
        require_manager(request, match.tournament)
        serializer = CupPlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data.copy()
        if data.pop("expected_revision") != match.revision:
            raise Conflict()
        try:
            plan_cup_match(match, CupMatchPlan(**data))
        except CupError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        touch_tournament(match.tournament)
        return Response(build_tournament_snapshot(match.tournament))
