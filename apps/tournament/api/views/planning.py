"""Tournament planning API endpoints and helpers."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.serializers import (
    GenerationRequestSerializer,
    MatchGenerationRequestSerializer,
    PoolGenerationRequestSerializer,
    TournamentMatchWriteSerializer,
    TournamentPoolWriteSerializer,
    TournamentScheduleImportSerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import Tournament, TournamentMatch, TournamentPool
from apps.tournament.services.editing import (
    MatchDraft,
    TournamentEditingError,
    create_pool,
    delete_match,
    delete_pool,
    save_match,
    update_pool,
    update_pool_order,
)
from apps.tournament.services.generation import (
    GenerationError,
    GenerationOptions,
    apply_existing_pool_match_plan,
    apply_generation_plan,
    apply_pool_plan,
    build_existing_pool_match_plan,
    build_generation_plan,
    build_pool_plan,
)
from apps.tournament.services.importing import (
    ImportedScheduleRow,
    ScheduleImportError,
    apply_imported_schedule,
)
from apps.tournament.services.snapshot import build_tournament_snapshot

from .common import (
    editing_error_response,
    get_tournament,
    require_manager,
    resolve_qualifiers,
)


def _validated_generation(
    request: Request,
    tournament: Tournament,
) -> tuple[dict[str, Any], dict[str, Any]]:
    serializer = GenerationRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    params = dict(serializer.validated_data)
    try:
        plan = build_generation_plan(tournament, options=GenerationOptions(**params))
    except GenerationError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    return params, plan


def _persist_generation_defaults(
    tournament: Tournament,
    params: dict[str, Any],
) -> None:
    """Keep later planning steps aligned with the applied schedule settings."""
    field_mapping = {
        "duration_minutes": "match_duration_minutes",
        "changeover_minutes": "changeover_minutes",
        "minimum_rest_minutes": "minimum_rest_minutes",
    }
    update_fields = []
    for parameter, model_field in field_mapping.items():
        if parameter not in params:
            continue
        setattr(tournament, model_field, params[parameter])
        update_fields.append(model_field)
    if update_fields:
        tournament.save(update_fields=[*update_fields, "updated_at"])


class TournamentGenerationPreviewView(APIView):
    """Preview pool allocation and scheduling without database changes."""

    @extend_schema(
        request=GenerationRequestSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Return a deterministic plan without modifying the tournament."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        _, plan = _validated_generation(request, tournament)
        return Response(plan)


class TournamentGenerationApplyView(APIView):
    """Apply the same server-calculated plan shown in preview."""

    @extend_schema(
        request=GenerationRequestSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def post(self, request: Request, tournament_id: str) -> Response:
        """Generate and atomically apply the reviewed schedule parameters."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        params, plan = _validated_generation(request, tournament)
        try:
            apply_generation_plan(tournament, plan=plan)
        except GenerationError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        _persist_generation_defaults(tournament, params)
        touch_tournament(tournament)
        tournament = Tournament.objects.select_related("display_config").get(
            pk=tournament.pk
        )
        return Response(build_tournament_snapshot(tournament))


class TournamentScheduleImportView(APIView):
    """Import the pool and fixture plan of an existing tournament."""

    @extend_schema(
        request=TournamentScheduleImportSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Create missing teams and fields and apply the supplied schedule.

        Raises:
            ValidationError: If the imported rows are internally inconsistent.

        """
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentScheduleImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rows = [ImportedScheduleRow(**row) for row in serializer.validated_data["rows"]]
        try:
            apply_imported_schedule(tournament, rows=rows)
        except ScheduleImportError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        except GenerationError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        touch_tournament(tournament)
        tournament = Tournament.objects.select_related("display_config").get(
            pk=tournament.pk
        )
        return Response(build_tournament_snapshot(tournament))


def _match_draft(
    tournament: Tournament,
    values: dict[str, Any],
    *,
    match: TournamentMatch | None = None,
) -> MatchDraft:
    local_start = (
        match.starts_at.astimezone(ZoneInfo(tournament.timezone))
        if match and match.starts_at
        else None
    )
    match_date = values.get("date", local_start.date() if local_start else None)
    start_time = values.get(
        "start_time", local_start.time().replace(tzinfo=None) if local_start else None
    )
    if not isinstance(match_date, date) or not isinstance(start_time, time):
        raise ValidationError({"detail": "Complete the match date and start time."})
    defaults = {
        "pool_id": match.pool_id if match else None,
        "home_team_id": match.home_team_id if match else None,
        "away_team_id": match.away_team_id if match else None,
        "field_id": match.field_id if match else None,
        "duration_minutes": match.duration_minutes if match else None,
        "round_number": match.round_number if match else None,
    }
    resolved = {key: values.get(key, value) for key, value in defaults.items()}
    if any(value is None for value in resolved.values()):
        raise ValidationError({"detail": "Complete all match planning fields."})
    return MatchDraft(
        pool_id=resolved["pool_id"],
        home_team_id=resolved["home_team_id"],
        away_team_id=resolved["away_team_id"],
        field_id=resolved["field_id"],
        starts_at=datetime.combine(
            match_date,
            start_time,
            tzinfo=ZoneInfo(tournament.timezone),
        ),
        duration_minutes=resolved["duration_minutes"],
        round_number=resolved["round_number"],
    )


class TournamentPoolListCreateView(APIView):
    """Create organizer-reviewed pools manually."""

    @extend_schema(
        request=TournamentPoolWriteSerializer, responses={201: OpenApiTypes.OBJECT}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Create a pool and assign its ordered teams."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentPoolWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            create_pool(tournament, **serializer.validated_data)
        except TournamentEditingError as exc:
            return editing_error_response(exc)
        touch_tournament(tournament)
        return Response(
            build_tournament_snapshot(tournament),
            status=status.HTTP_201_CREATED,
        )


class TournamentPoolDetailView(APIView):
    """Edit or delete one reviewed pool."""

    def _objects(
        self, tournament_id: str, pool_id: str
    ) -> tuple[Tournament, TournamentPool]:
        tournament = get_tournament(tournament_id)
        return tournament, get_object_or_404(tournament.pools, id_uuid=pool_id)

    @extend_schema(
        request=TournamentPoolWriteSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    def patch(self, request: Request, tournament_id: str, pool_id: str) -> Response:
        """Replace a pool's details or presentation order."""
        tournament, pool = self._objects(tournament_id, pool_id)
        require_manager(request, tournament)
        serializer = TournamentPoolWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if set(serializer.validated_data) == {"sort_order"}:
            update_pool_order(
                tournament,
                pool,
                sort_order=serializer.validated_data["sort_order"],
            )
            touch_tournament(tournament)
            return Response(build_tournament_snapshot(tournament))
        values = {
            "name": serializer.validated_data.get("name", pool.name),
            "assigned_field_id": serializer.validated_data.get(
                "assigned_field_id", pool.assigned_field_id
            ),
            "team_ids": serializer.validated_data.get(
                "team_ids",
                list(pool.entries.values_list("team_id", flat=True)),
            ),
        }
        try:
            update_pool(tournament, pool, **values)
            if "sort_order" in serializer.validated_data:
                update_pool_order(
                    tournament,
                    pool,
                    sort_order=serializer.validated_data["sort_order"],
                )
        except TournamentEditingError as exc:
            return editing_error_response(exc)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))

    @extend_schema(request=None, responses={204: None})
    def delete(self, request: Request, tournament_id: str, pool_id: str) -> Response:
        """Delete a pool while its match schedule is empty."""
        tournament, pool = self._objects(tournament_id, pool_id)
        require_manager(request, tournament)
        try:
            delete_pool(tournament, pool)
        except TournamentEditingError as exc:
            return editing_error_response(exc)
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentPoolsGenerateView(APIView):
    """Generate editable pools without also creating matches."""

    @extend_schema(
        request=PoolGenerationRequestSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    def post(self, request: Request, tournament_id: str) -> Response:
        """Replace draft pools with a generated allocation for review.

        Raises:
            ValidationError: If the requested pool allocation is invalid.

        """
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = PoolGenerationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            plan = build_pool_plan(tournament, **serializer.validated_data)
        except GenerationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        try:
            apply_pool_plan(tournament, pool_plan=plan)
        except GenerationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))


class TournamentMatchListCreateView(APIView):
    """Create reviewed tournament matches manually."""

    @extend_schema(
        request=TournamentMatchWriteSerializer, responses={201: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def post(self, request: Request, tournament_id: str) -> Response:
        """Create one conflict-free pool match."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentMatchWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            save_match(
                tournament,
                draft=_match_draft(tournament, serializer.validated_data),
            )
        except TournamentEditingError as exc:
            return editing_error_response(exc)
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return Response(
            build_tournament_snapshot(tournament),
            status=status.HTTP_201_CREATED,
        )


class TournamentMatchDetailView(APIView):
    """Edit or delete one unstarted tournament match."""

    def _objects(
        self, tournament_id: str, match_id: str
    ) -> tuple[Tournament, TournamentMatch]:
        tournament = get_tournament(tournament_id)
        return tournament, get_object_or_404(tournament.matches, id_uuid=match_id)

    @extend_schema(
        request=TournamentMatchWriteSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def patch(self, request: Request, tournament_id: str, match_id: str) -> Response:
        """Replace selected match planning fields."""
        tournament, match = self._objects(tournament_id, match_id)
        require_manager(request, tournament)
        serializer = TournamentMatchWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        draft = _match_draft(tournament, serializer.validated_data, match=match)
        try:
            save_match(tournament, match=match, draft=draft)
        except TournamentEditingError as exc:
            return editing_error_response(exc)
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))

    @extend_schema(request=None, responses={204: None})
    @transaction.atomic
    def delete(self, request: Request, tournament_id: str, match_id: str) -> Response:
        """Delete one draft match."""
        tournament, match = self._objects(tournament_id, match_id)
        require_manager(request, tournament)
        try:
            delete_match(tournament, match)
        except TournamentEditingError as exc:
            return editing_error_response(exc)
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentMatchesGenerateView(APIView):
    """Generate editable matches from reviewed pools."""

    @extend_schema(
        request=MatchGenerationRequestSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def post(self, request: Request, tournament_id: str) -> Response:
        """Replace draft matches while retaining the current pools.

        Raises:
            ValidationError: If the pool or timing configuration is invalid.

        """
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = MatchGenerationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        options = GenerationOptions(
            pool_count=tournament.pools.count(),
            **serializer.validated_data,
        )
        try:
            plan = build_existing_pool_match_plan(tournament, options=options)
        except GenerationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        try:
            apply_existing_pool_match_plan(tournament, matches=plan)
        except GenerationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        _persist_generation_defaults(tournament, serializer.validated_data)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))
