"""Tournament finals API endpoints and helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.serializers import (
    FinalsGenerationSerializer,
    TournamentFinalGroupWriteSerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import Tournament, TournamentFinalGroup
from apps.tournament.services.final_groups import (
    FinalGroupError,
    FinalGroupPlan,
    FinalMatchPlan,
    create_final_group,
    delete_final_group,
)
from apps.tournament.services.finals import generate_finals
from apps.tournament.services.generation import GenerationError
from apps.tournament.services.snapshot import build_tournament_snapshot

from .common import get_tournament, require_manager


class TournamentFinalsGenerateView(APIView):
    """Plan a knockout bracket whose entrants resolve from pool standings."""

    @extend_schema(
        request=FinalsGenerationSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Create and return a single-elimination finals stage.

        Raises:
            ValidationError: If pool play or qualifier counts are invalid.

        """
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = FinalsGenerationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            generate_finals(tournament, **serializer.validated_data)
        except GenerationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        touch_tournament(tournament)
        tournament = Tournament.objects.select_related("display_config").get(
            pk=tournament.pk
        )
        return Response(build_tournament_snapshot(tournament))


def _final_match_plan(
    tournament: Tournament,
    values: dict[str, Any],
) -> FinalMatchPlan:
    return FinalMatchPlan(
        field_id=values["field_id"],
        starts_at=datetime.combine(
            values["date"],
            values["start_time"],
            tzinfo=ZoneInfo(tournament.timezone),
        ),
        duration_minutes=values["duration_minutes"],
    )


class TournamentFinalGroupListCreateView(APIView):
    """Plan an independently qualified four-team finals bracket."""

    @extend_schema(
        request=TournamentFinalGroupWriteSerializer,
        responses={201: OpenApiTypes.OBJECT},
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Create a reviewable final group before or after pool completion.

        Raises:
            ValidationError: If the requested group cannot be planned safely.

        """
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentFinalGroupWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        plan = FinalGroupPlan(
            name=values["name"],
            format=values["format"],
            pool_ids=tuple(values["pool_ids"]),
            semifinals=(
                _final_match_plan(tournament, values["semifinals"][0]),
                _final_match_plan(tournament, values["semifinals"][1]),
            ),
            final=_final_match_plan(tournament, values["final"]),
        )
        try:
            create_final_group(tournament, plan=plan)
        except FinalGroupError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        touch_tournament(tournament)
        tournament = Tournament.objects.select_related("display_config").get(
            pk=tournament.pk
        )
        return Response(
            build_tournament_snapshot(tournament),
            status=status.HTTP_201_CREATED,
        )


class TournamentFinalGroupDetailView(APIView):
    """Remove one unstarted final group without touching pool play."""

    @extend_schema(request=None, responses={204: None})
    def delete(
        self,
        request: Request,
        tournament_id: str,
        group_id: str,
    ) -> Response:
        """Delete the bracket when none of its matches has live data."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        group = get_object_or_404(
            TournamentFinalGroup,
            tournament=tournament,
            id_uuid=group_id,
        )
        try:
            delete_final_group(tournament, group)
        except FinalGroupError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)
