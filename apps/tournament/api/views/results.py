"""Tournament results API endpoints and helpers."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.permissions import can_score_match
from apps.tournament.api.schema import DeleteBodySchema
from apps.tournament.api.serializers import (
    TournamentRefereeReadySerializer,
    TournamentResultSerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import (
    TournamentMatch,
    TournamentResultAudit,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.match_operations import (
    TournamentMatchOperationError,
    downstream_result_locked,
    reset_field_readiness,
    reset_match_state,
    start_round,
    sync_advanced_winner,
)
from apps.tournament.services.referee_tracker import (
    RefereeTrackerError,
    mark_field_ready,
)
from apps.tournament.services.snapshot import build_tournament_snapshot

from .common import (
    Conflict,
    lock_tournament,
    lock_tournament_for_match,
    require_authentication,
    require_manager,
    resolve_qualifiers,
)


def _result_winner(
    match: TournamentMatch,
    result: dict[str, Any],
) -> TournamentTeam | None:
    home_score = result["home_score"]
    away_score = result["away_score"]
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return match.home_team
    if away_score > home_score:
        return match.away_team
    winner_id = result.get("winner_id")
    if not winner_id:
        return None
    if str(winner_id) == str(match.home_team_id):
        return match.home_team
    if str(winner_id) == str(match.away_team_id):
        return match.away_team
    raise ValidationError({"winner_id": "Winner must be a participating team."})


class TournamentMatchResultView(APIView):
    """Enter, finalize, reopen, or correct one match result."""

    @extend_schema(
        request=TournamentResultSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def patch(self, request: Request, match_id: str) -> Response:
        """Apply one optimistic-lock result update and record its history.

        Raises:
            PermissionDenied: If the viewer cannot score the selected match.
            Conflict: If another device has already changed the result.
            ValidationError: If scores or a knockout winner are invalid.

        """
        tournament = lock_tournament_for_match(match_id)
        match = get_object_or_404(
            TournamentMatch.objects.select_for_update(of=("self",)).select_related(
                "tournament", "stage", "field", "home_team", "away_team"
            ),
            id_uuid=match_id,
            tournament=tournament,
        )
        require_authentication(request)
        if not can_score_match(request.user, match):
            raise PermissionDenied("You cannot score this match.")
        serializer = TournamentResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["expected_revision"] != match.revision:
            raise Conflict()
        if tournament.cup_rules:
            raise ValidationError({
                "detail": "Gebruik de bekertracker om periodes en strafworpen "
                "afzonderlijk vast te leggen."
            })
        if match.home_team is None or match.away_team is None:
            return Response(
                {"detail": "Both teams must be known before entering a result."},
                status=status.HTTP_409_CONFLICT,
            )
        if match.next_match_id:
            match.next_match = TournamentMatch.objects.select_for_update(
                of=("self",)
            ).get(pk=match.next_match_id)

        home_score = data["home_score"]
        away_score = data["away_score"]
        winner = _result_winner(match, data)
        target_winner = (
            winner if data["status"] == TournamentMatch.Status.FINAL else None
        )
        if (
            data["status"] == TournamentMatch.Status.FINAL
            and match.stage.kind != TournamentStage.Kind.POOL
            and winner is None
        ):
            raise ValidationError({
                "winner_id": "A knockout result must identify a winner."
            })
        if downstream_result_locked(match, target_winner):
            return Response(
                {
                    "detail": (
                        "The next bracket match already started. Reopen it before "
                        "correcting this winner."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        TournamentResultAudit.objects.create(
            match=match,
            previous_home_score=match.home_score,
            previous_away_score=match.away_score,
            new_home_score=home_score,
            new_away_score=away_score,
            previous_status=match.status,
            new_status=data["status"],
            reason=data.get("reason", ""),
            changed_by=request.user,
            changed_by_name=str(request.user),
            source=TournamentResultAudit.Source.DIRECT,
        )
        match.home_score = home_score
        match.away_score = away_score
        match.status = data["status"]
        match.winner = target_winner
        match.revision += 1
        match.save(
            update_fields=[
                "home_score",
                "away_score",
                "status",
                "winner",
                "revision",
                "updated_at",
            ]
        )

        sync_advanced_winner(match)

        if match.stage.kind == TournamentStage.Kind.POOL:
            resolve_qualifiers(tournament)

        touch_tournament(tournament)
        return Response({
            "id_uuid": str(match.id_uuid),
            "home_score": match.home_score,
            "away_score": match.away_score,
            "status": match.status,
            "winner_id": str(match.winner_id) if match.winner_id else None,
            "revision": match.revision,
        })


class TournamentMatchReadinessView(APIView):
    """Let a manager set or revoke a field-readiness signal."""

    schema = DeleteBodySchema()

    @extend_schema(
        request=TournamentRefereeReadySerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Mark a scheduled match ready with optimistic locking.

        Raises:
            Conflict: If the match changed after the manager loaded it.

        """
        serializer = TournamentRefereeReadySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tournament = lock_tournament_for_match(match_id)
        match = get_object_or_404(
            TournamentMatch.objects.select_for_update(of=("self",)).select_related(
                "tournament"
            ),
            id_uuid=match_id,
            tournament=tournament,
        )
        require_manager(request, match.tournament)
        if match.field_ready_at is not None:
            return Response({"id_uuid": str(match.id_uuid), "revision": match.revision})
        if serializer.validated_data["expected_revision"] != match.revision:
            raise Conflict()
        if match.status != TournamentMatch.Status.SCHEDULED:
            return Response(
                {"detail": "Alleen een geplande wedstrijd kan gereed worden gemeld."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            mark_field_ready(
                match,
                actor=request.user,
                actor_name=str(request.user),
            )
        except RefereeTrackerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        touch_tournament(tournament)
        return Response({"id_uuid": str(match.id_uuid), "revision": match.revision})

    @extend_schema(
        request=TournamentRefereeReadySerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def delete(self, request: Request, match_id: str) -> Response:
        """Reset readiness while the match is still scheduled.

        Raises:
            Conflict: If the match changed after the manager loaded it.

        """
        serializer = TournamentRefereeReadySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tournament = lock_tournament_for_match(match_id)
        match = get_object_or_404(
            TournamentMatch.objects.select_for_update(of=("self",)).select_related(
                "tournament"
            ),
            id_uuid=match_id,
            tournament=tournament,
        )
        require_manager(request, match.tournament)
        if match.field_ready_at is None:
            return Response({"id_uuid": str(match.id_uuid), "revision": match.revision})
        if serializer.validated_data["expected_revision"] != match.revision:
            raise Conflict()
        try:
            changed = reset_field_readiness(match)
        except TournamentMatchOperationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        if changed:
            touch_tournament(tournament)
        return Response({"id_uuid": str(match.id_uuid), "revision": match.revision})


class TournamentMatchStateResetView(APIView):
    """Move any tournament match back to its previous lifecycle state."""

    @extend_schema(
        request=TournamentRefereeReadySerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Reset one manager-controlled match with optimistic locking.

        Raises:
            Conflict: If the match changed or a downstream result blocks reset.

        """
        serializer = TournamentRefereeReadySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tournament = lock_tournament_for_match(match_id)
        match = get_object_or_404(
            TournamentMatch.objects.select_for_update(of=("self",)).select_related(
                "tournament", "stage"
            ),
            id_uuid=match_id,
            tournament=tournament,
        )
        require_manager(request, match.tournament)
        if serializer.validated_data["expected_revision"] != match.revision:
            raise Conflict()
        if match.next_match_id:
            match.next_match = TournamentMatch.objects.select_for_update(
                of=("self",)
            ).get(pk=match.next_match_id)
        try:
            changed = reset_match_state(match, actor=request.user)
        except TournamentMatchOperationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        if changed and match.stage.kind == TournamentStage.Kind.POOL:
            resolve_qualifiers(tournament)
        if changed:
            touch_tournament(tournament)
        return Response({
            "id_uuid": str(match.id_uuid),
            "home_score": match.home_score,
            "away_score": match.away_score,
            "status": match.status,
            "winner_id": str(match.winner_id) if match.winner_id else None,
            "revision": match.revision,
        })


class TournamentRoundStartView(APIView):
    """Start all ready matches in one tournament round together."""

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    @transaction.atomic
    def post(
        self,
        request: Request,
        tournament_id: str,
        stage_id: str,
        round_number: int,
    ) -> Response:
        """Move the round's scheduled matches to live in one locked operation.

        Raises:
            NotFound: If the requested round has no matches.

        """
        tournament = lock_tournament(tournament_id)
        require_manager(request, tournament)
        if tournament.cup_rules:
            return Response(
                {
                    "detail": "Start bekerwedstrijden afzonderlijk "
                    "zodra hun veld vrij is."
                },
                status=status.HTTP_409_CONFLICT,
            )
        matches = list(
            TournamentMatch.objects
            .select_for_update(of=("self",))
            .filter(
                tournament=tournament,
                stage_id=stage_id,
                round_number=round_number,
            )
            .select_related("home_team", "away_team")
            .order_by("match_number")
        )
        if not matches:
            raise NotFound("Deze ronde bestaat niet.")
        try:
            start_round(matches, actor=request.user)
        except TournamentMatchOperationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))
