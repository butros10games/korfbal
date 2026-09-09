"""Tournament resources API endpoints and helpers."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.serializers import (
    TournamentFieldSerializer,
    TournamentMemberSerializer,
    TournamentStandingAdjustmentSerializer,
    TournamentTeamSerializer,
    TournamentTeamSubstitutionSerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMember,
    TournamentStandingAdjustment,
    TournamentTeam,
)
from apps.tournament.services.editing import (
    MatchSubstitution,
    TournamentEditingError,
    substitute_absent_team,
)
from apps.tournament.services.snapshot import build_tournament_snapshot

from .common import (
    editing_error_response,
    get_tournament,
    require_manager,
    resolve_qualifiers,
)


class TournamentTeamListCreateView(APIView):
    """List and add custom teams within one tournament."""

    @extend_schema(responses={200: TournamentTeamSerializer(many=True)})
    def get(self, request: Request, tournament_id: str) -> Response:
        """List custom teams for a tournament manager."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        return Response(
            TournamentTeamSerializer(tournament.teams.all(), many=True).data
        )

    @extend_schema(
        request=TournamentTeamSerializer, responses={201: TournamentTeamSerializer}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Add one custom tournament team."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentTeamSerializer(
            data=request.data,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        team = serializer.save(tournament=tournament)
        touch_tournament(tournament)
        return Response(
            TournamentTeamSerializer(team).data,
            status=status.HTTP_201_CREATED,
        )


class TournamentTeamDetailView(APIView):
    """Edit or remove an unused custom team."""

    def _objects(
        self, tournament_id: str, team_id: str
    ) -> tuple[Tournament, TournamentTeam]:
        tournament = get_tournament(tournament_id)
        team = get_object_or_404(tournament.teams, id_uuid=team_id)
        return tournament, team

    @extend_schema(
        request=TournamentTeamSerializer, responses={200: TournamentTeamSerializer}
    )
    @transaction.atomic
    def patch(self, request: Request, tournament_id: str, team_id: str) -> Response:
        """Update a custom team's name, seed, or operational state."""
        tournament, team = self._objects(tournament_id, team_id)
        require_manager(request, tournament)
        serializer = TournamentTeamSerializer(
            team,
            data=request.data,
            partial=True,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return Response(serializer.data)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request: Request, tournament_id: str, team_id: str) -> Response:
        """Delete a team only while no schedule references it."""
        tournament, team = self._objects(tournament_id, team_id)
        require_manager(request, tournament)
        if tournament.matches.filter(Q(home_team=team) | Q(away_team=team)).exists():
            return Response(
                {"detail": "Withdraw teams that already have scheduled matches."},
                status=status.HTTP_409_CONFLICT,
            )
        team.delete()
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentTeamSubstitutionView(APIView):
    """Replace an absent team's remaining pool fixtures with guest teams."""

    @extend_schema(
        request=TournamentTeamSubstitutionSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def post(self, request: Request, tournament_id: str, team_id: UUID) -> Response:
        """Apply a complete, conflict-free last-minute replacement plan."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentTeamSubstitutionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        substitutions = [
            MatchSubstitution(**replacement)
            for replacement in serializer.validated_data["replacements"]
        ]
        referee_substitutions = [
            MatchSubstitution(**replacement)
            for replacement in serializer.validated_data["referee_replacements"]
        ]
        try:
            substitute_absent_team(
                tournament,
                absent_team_id=team_id,
                substitutions=substitutions,
                referee_substitutions=referee_substitutions,
            )
        except TournamentEditingError as exc:
            return editing_error_response(exc)
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))


class TournamentFieldListCreateView(APIView):
    """List and add tournament fields."""

    @extend_schema(responses={200: TournamentFieldSerializer(many=True)})
    def get(self, request: Request, tournament_id: str) -> Response:
        """List configured tournament fields."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        return Response(
            TournamentFieldSerializer(tournament.fields.all(), many=True).data
        )

    @extend_schema(
        request=TournamentFieldSerializer, responses={201: TournamentFieldSerializer}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Add a labeled tournament field."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentFieldSerializer(
            data=request.data,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        field = serializer.save(tournament=tournament)
        touch_tournament(tournament)
        return Response(
            TournamentFieldSerializer(field).data,
            status=status.HTTP_201_CREATED,
        )


class TournamentFieldDetailView(APIView):
    """Edit or remove an unused tournament field."""

    def _objects(
        self, tournament_id: str, field_id: str
    ) -> tuple[Tournament, TournamentField]:
        tournament = get_tournament(tournament_id)
        field = get_object_or_404(tournament.fields, id_uuid=field_id)
        return tournament, field

    @extend_schema(
        request=TournamentFieldSerializer, responses={200: TournamentFieldSerializer}
    )
    def patch(self, request: Request, tournament_id: str, field_id: str) -> Response:
        """Update a field label, order, or active state."""
        tournament, field = self._objects(tournament_id, field_id)
        require_manager(request, tournament)
        serializer = TournamentFieldSerializer(
            field,
            data=request.data,
            partial=True,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        touch_tournament(tournament)
        return Response(serializer.data)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request: Request, tournament_id: str, field_id: str) -> Response:
        """Delete a field only while no scheduled match references it."""
        tournament, field = self._objects(tournament_id, field_id)
        require_manager(request, tournament)
        if field.matches.exists():
            return Response(
                {"detail": "Deactivate fields that already have scheduled matches."},
                status=status.HTTP_409_CONFLICT,
            )
        field.delete()
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentMemberListCreateView(APIView):
    """List and grant tournament collaboration roles."""

    @extend_schema(responses={200: TournamentMemberSerializer(many=True)})
    def get(self, request: Request, tournament_id: str) -> Response:
        """List managers and scorekeepers for a tournament."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        members = tournament.member_roles.select_related("user", "field")
        return Response(TournamentMemberSerializer(members, many=True).data)

    @extend_schema(
        request=TournamentMemberSerializer, responses={201: TournamentMemberSerializer}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Grant one manager or field-scoped scorekeeper role."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentMemberSerializer(
            data=request.data,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        member = serializer.save(tournament=tournament)
        return Response(
            TournamentMemberSerializer(member).data,
            status=status.HTTP_201_CREATED,
        )


class TournamentMemberDetailView(APIView):
    """Change or revoke one collaboration role."""

    def _objects(
        self, tournament_id: str, member_id: int
    ) -> tuple[Tournament, TournamentMember]:
        tournament = get_tournament(tournament_id)
        member = get_object_or_404(tournament.member_roles, pk=member_id)
        return tournament, member

    @extend_schema(
        request=TournamentMemberSerializer, responses={200: TournamentMemberSerializer}
    )
    def patch(self, request: Request, tournament_id: str, member_id: int) -> Response:
        """Change role or assigned field."""
        tournament, member = self._objects(tournament_id, member_id)
        require_manager(request, tournament)
        serializer = TournamentMemberSerializer(
            member,
            data=request.data,
            partial=True,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request: Request, tournament_id: str, member_id: int) -> Response:
        """Revoke a role without affecting result history."""
        tournament, member = self._objects(tournament_id, member_id)
        require_manager(request, tournament)
        member.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentStandingAdjustmentListCreateView(APIView):
    """List or add audited pool-table bonuses and penalties."""

    @extend_schema(responses={200: TournamentStandingAdjustmentSerializer(many=True)})
    def get(self, request: Request, tournament_id: str) -> Response:
        """List all standings adjustments for managers."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        adjustments = TournamentStandingAdjustment.objects.filter(
            entry__pool__tournament=tournament
        ).select_related("entry", "created_by")
        return Response(
            TournamentStandingAdjustmentSerializer(adjustments, many=True).data
        )

    @extend_schema(
        request=TournamentStandingAdjustmentSerializer,
        responses={201: TournamentStandingAdjustmentSerializer},
    )
    @transaction.atomic
    def post(self, request: Request, tournament_id: str) -> Response:
        """Apply a reasoned points adjustment to one pool entry."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentStandingAdjustmentSerializer(
            data=request.data,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        adjustment = serializer.save(created_by=request.user)
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return Response(
            TournamentStandingAdjustmentSerializer(adjustment).data,
            status=status.HTTP_201_CREATED,
        )


class TournamentStandingAdjustmentDetailView(APIView):
    """Remove an incorrect standings adjustment."""

    @extend_schema(request=None, responses={204: None})
    @transaction.atomic
    def delete(
        self, request: Request, tournament_id: str, adjustment_id: str
    ) -> Response:
        """Remove an adjustment and refresh public standings."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        adjustment = get_object_or_404(
            TournamentStandingAdjustment,
            id_uuid=adjustment_id,
            entry__pool__tournament=tournament,
        )
        adjustment.delete()
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)
