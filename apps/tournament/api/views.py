"""REST endpoints for tournament planning, scoring, and presentation."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.shortcuts import get_object_or_404
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.exceptions import (
    APIException,
    NotAuthenticated,
    PermissionDenied,
    ValidationError,
)
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.permissions import (
    can_manage_tournament,
    can_score_match,
    is_authenticated,
)
from apps.tournament.api.serializers import (
    FinalsGenerationSerializer,
    GenerationRequestSerializer,
    MatchGenerationRequestSerializer,
    PoolGenerationRequestSerializer,
    TournamentDisplayConfigSerializer,
    TournamentFieldSerializer,
    TournamentFinalGroupWriteSerializer,
    TournamentMatchWriteSerializer,
    TournamentMemberSerializer,
    TournamentPoolWriteSerializer,
    TournamentRefereeGoalSerializer,
    TournamentRefereeReadySerializer,
    TournamentResultSerializer,
    TournamentScheduleImportSerializer,
    TournamentSerializer,
    TournamentStandingAdjustmentSerializer,
    TournamentTeamSerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentFinalGroup,
    TournamentMatch,
    TournamentMember,
    TournamentPool,
    TournamentResultAudit,
    TournamentStage,
    TournamentStandingAdjustment,
    TournamentTeam,
)
from apps.tournament.services.editing import (
    MatchDraft,
    TournamentEditingError,
    create_pool,
    delete_match,
    delete_pool,
    save_match,
    update_pool,
)
from apps.tournament.services.final_groups import (
    FinalGroupError,
    FinalGroupPlan,
    FinalMatchPlan,
    create_final_group,
    delete_final_group,
    resolve_final_group_qualifiers,
)
from apps.tournament.services.finals import generate_finals
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
from apps.tournament.services.referee_tracker import (
    RefereeTrackerError,
    build_referee_tracker_state,
    mark_field_ready,
    record_goal,
)
from apps.tournament.services.snapshot import build_tournament_snapshot


PUBLIC_STATUSES = {
    Tournament.Status.PUBLISHED,
    Tournament.Status.LIVE,
    Tournament.Status.FINISHED,
}
REFEREE_REVISION_CONFLICT_DETAIL = (
    "De wedstrijd is elders gewijzigd. De nieuwste stand wordt getoond."
)


class Conflict(APIException):
    """A stale client tried to overwrite a newer result."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "This result changed on another device. Refresh and try again."


def _require_authentication(request: Request) -> None:
    if not is_authenticated(request.user):
        raise NotAuthenticated()


def _require_manager(request: Request, tournament: Tournament) -> None:
    _require_authentication(request)
    if not can_manage_tournament(request.user, tournament):
        raise PermissionDenied("You do not have permission to manage this tournament.")


def _get_tournament(tournament_id: str) -> Tournament:
    return get_object_or_404(
        Tournament.objects.select_related("owner", "organizer_club"),
        id_uuid=tournament_id,
    )


def _public_access_allowed(request: Request, tournament: Tournament) -> bool:
    if can_manage_tournament(request.user, tournament):
        return True
    if tournament.status not in PUBLIC_STATUSES:
        return False
    if tournament.visibility == Tournament.Visibility.PUBLIC:
        return True
    return request.query_params.get("token") == str(tournament.display_token)


class TournamentViewSet(
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """List public/owned tournaments and manage their rules."""

    serializer_class = TournamentSerializer
    permission_classes = (permissions.AllowAny,)
    lookup_field = "id_uuid"
    http_method_names = ("get", "post", "patch", "head", "options")

    def get_queryset(self) -> QuerySet[Tournament]:
        """Return public tournaments plus events manageable by the viewer."""
        queryset = (
            Tournament.objects
            .select_related("owner", "organizer_club")
            .annotate(
                team_count=Count("teams", distinct=True),
                field_count=Count("fields", distinct=True),
                match_count=Count("matches", distinct=True),
            )
            .order_by("-starts_at", "name")
        )
        user = self.request.user
        public = Q(
            visibility=Tournament.Visibility.PUBLIC,
            status__in=PUBLIC_STATUSES,
        )
        if not is_authenticated(user):
            return queryset.filter(public)
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return queryset
        return queryset.filter(public | Q(owner=user) | Q(members=user)).distinct()

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Create a tournament owned by the authenticated viewer."""
        _require_authentication(request)
        return super().create(request, *args, **kwargs)

    def update(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Replace editable tournament fields for a manager."""
        tournament = self.get_object()
        _require_manager(request, tournament)
        response = super().update(request, *args, **kwargs)
        touch_tournament(tournament)
        return response

    def partial_update(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """Patch editable tournament fields for a manager."""
        tournament = self.get_object()
        _require_manager(request, tournament)
        response = super().partial_update(request, *args, **kwargs)
        touch_tournament(tournament)
        return response


class TournamentPublicView(APIView):
    """Return the public tournament snapshot by readable slug."""

    permission_classes = (permissions.AllowAny,)

    def get(self, request: Request, slug: str) -> Response:
        """Return a published snapshot when visibility permits it.

        Raises:
            PermissionDenied: If the viewer lacks public or manager access.

        """
        tournament = get_object_or_404(
            Tournament.objects.select_related("display_config"), slug=slug
        )
        if not _public_access_allowed(request, tournament):
            raise PermissionDenied("This tournament display is not public.")
        return Response(build_tournament_snapshot(tournament))


class TournamentSnapshotView(APIView):
    """Return a management snapshot, including draft tournaments."""

    permission_classes = (permissions.AllowAny,)

    def get(self, request: Request, tournament_id: str) -> Response:
        """Return the current snapshot and viewer capabilities.

        Raises:
            PermissionDenied: If the viewer lacks public or manager access.

        """
        tournament = _get_tournament(tournament_id)
        if not _public_access_allowed(request, tournament):
            raise PermissionDenied("This tournament is not available.")
        tournament = Tournament.objects.select_related("display_config").get(
            pk=tournament.pk
        )
        payload = build_tournament_snapshot(tournament)
        can_manage = can_manage_tournament(request.user, tournament)
        payload["capabilities"] = {
            "can_manage": can_manage,
            "display_token": str(tournament.display_token) if can_manage else None,
        }
        return Response(payload)


class TournamentTeamListCreateView(APIView):
    """List and add custom teams within one tournament."""

    def get(self, request: Request, tournament_id: str) -> Response:
        """List custom teams for a tournament manager."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        return Response(
            TournamentTeamSerializer(tournament.teams.all(), many=True).data
        )

    def post(self, request: Request, tournament_id: str) -> Response:
        """Add one custom tournament team."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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
        tournament = _get_tournament(tournament_id)
        team = get_object_or_404(tournament.teams, id_uuid=team_id)
        return tournament, team

    def patch(self, request: Request, tournament_id: str, team_id: str) -> Response:
        """Update a custom team's name, seed, or operational state."""
        tournament, team = self._objects(tournament_id, team_id)
        _require_manager(request, tournament)
        serializer = TournamentTeamSerializer(
            team,
            data=request.data,
            partial=True,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        touch_tournament(tournament)
        return Response(serializer.data)

    def delete(self, request: Request, tournament_id: str, team_id: str) -> Response:
        """Delete a team only while no schedule references it."""
        tournament, team = self._objects(tournament_id, team_id)
        _require_manager(request, tournament)
        if tournament.matches.filter(Q(home_team=team) | Q(away_team=team)).exists():
            return Response(
                {"detail": "Withdraw teams that already have scheduled matches."},
                status=status.HTTP_409_CONFLICT,
            )
        team.delete()
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentFieldListCreateView(APIView):
    """List and add tournament fields."""

    def get(self, request: Request, tournament_id: str) -> Response:
        """List configured tournament fields."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        return Response(
            TournamentFieldSerializer(tournament.fields.all(), many=True).data
        )

    def post(self, request: Request, tournament_id: str) -> Response:
        """Add a labeled tournament field."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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
        tournament = _get_tournament(tournament_id)
        field = get_object_or_404(tournament.fields, id_uuid=field_id)
        return tournament, field

    def patch(self, request: Request, tournament_id: str, field_id: str) -> Response:
        """Update a field label, order, or active state."""
        tournament, field = self._objects(tournament_id, field_id)
        _require_manager(request, tournament)
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

    def delete(self, request: Request, tournament_id: str, field_id: str) -> Response:
        """Delete a field only while no scheduled match references it."""
        tournament, field = self._objects(tournament_id, field_id)
        _require_manager(request, tournament)
        if field.matches.exists():
            return Response(
                {"detail": "Deactivate fields that already have scheduled matches."},
                status=status.HTTP_409_CONFLICT,
            )
        field.delete()
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


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


class TournamentGenerationPreviewView(APIView):
    """Preview pool allocation and scheduling without database changes."""

    def post(self, request: Request, tournament_id: str) -> Response:
        """Return a deterministic plan without modifying the tournament."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        _, plan = _validated_generation(request, tournament)
        return Response(plan)


class TournamentGenerationApplyView(APIView):
    """Apply the same server-calculated plan shown in preview."""

    def post(self, request: Request, tournament_id: str) -> Response:
        """Generate and atomically apply the reviewed schedule parameters."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        _, plan = _validated_generation(request, tournament)
        try:
            apply_generation_plan(tournament, plan=plan)
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


class TournamentScheduleImportView(APIView):
    """Import the pool and fixture plan of an existing tournament."""

    def post(self, request: Request, tournament_id: str) -> Response:
        """Create missing teams and fields and apply the supplied schedule.

        Raises:
            ValidationError: If the imported rows are internally inconsistent.

        """
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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


def _editing_error_response(exc: TournamentEditingError) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)


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

    def post(self, request: Request, tournament_id: str) -> Response:
        """Create a pool and assign its ordered teams."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        serializer = TournamentPoolWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            create_pool(tournament, **serializer.validated_data)
        except TournamentEditingError as exc:
            return _editing_error_response(exc)
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
        tournament = _get_tournament(tournament_id)
        return tournament, get_object_or_404(tournament.pools, id_uuid=pool_id)

    def patch(self, request: Request, tournament_id: str, pool_id: str) -> Response:
        """Replace a pool's label or team assignment."""
        tournament, pool = self._objects(tournament_id, pool_id)
        _require_manager(request, tournament)
        serializer = TournamentPoolWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
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
        except TournamentEditingError as exc:
            return _editing_error_response(exc)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))

    def delete(self, request: Request, tournament_id: str, pool_id: str) -> Response:
        """Delete a pool while its match schedule is empty."""
        tournament, pool = self._objects(tournament_id, pool_id)
        _require_manager(request, tournament)
        try:
            delete_pool(tournament, pool)
        except TournamentEditingError as exc:
            return _editing_error_response(exc)
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentPoolsGenerateView(APIView):
    """Generate editable pools without also creating matches."""

    def post(self, request: Request, tournament_id: str) -> Response:
        """Replace draft pools with a generated allocation for review.

        Raises:
            ValidationError: If the requested pool allocation is invalid.

        """
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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

    def post(self, request: Request, tournament_id: str) -> Response:
        """Create one conflict-free pool match."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        serializer = TournamentMatchWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            save_match(
                tournament,
                draft=_match_draft(tournament, serializer.validated_data),
            )
        except TournamentEditingError as exc:
            return _editing_error_response(exc)
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
        tournament = _get_tournament(tournament_id)
        return tournament, get_object_or_404(tournament.matches, id_uuid=match_id)

    def patch(self, request: Request, tournament_id: str, match_id: str) -> Response:
        """Replace selected match planning fields."""
        tournament, match = self._objects(tournament_id, match_id)
        _require_manager(request, tournament)
        serializer = TournamentMatchWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        draft = _match_draft(tournament, serializer.validated_data, match=match)
        try:
            save_match(tournament, match=match, draft=draft)
        except TournamentEditingError as exc:
            return _editing_error_response(exc)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))

    def delete(self, request: Request, tournament_id: str, match_id: str) -> Response:
        """Delete one draft match."""
        tournament, match = self._objects(tournament_id, match_id)
        _require_manager(request, tournament)
        try:
            delete_match(tournament, match)
        except TournamentEditingError as exc:
            return _editing_error_response(exc)
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentMatchesGenerateView(APIView):
    """Generate editable matches from reviewed pools."""

    def post(self, request: Request, tournament_id: str) -> Response:
        """Replace draft matches while retaining the current pools.

        Raises:
            ValidationError: If the pool or timing configuration is invalid.

        """
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))


class TournamentPublishView(APIView):
    """Publish a complete generated tournament."""

    def post(self, request: Request, tournament_id: str) -> Response:
        """Publish a tournament after teams and matches exist."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        if not tournament.teams.filter(withdrawn=False).exists():
            return Response(
                {"detail": "Add active teams before publishing."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not tournament.matches.exists():
            return Response(
                {"detail": "Generate the schedule before publishing."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tournament.status = Tournament.Status.PUBLISHED
        tournament.save(update_fields=["status", "updated_at"])
        touch_tournament(tournament)
        return Response(
            TournamentSerializer(tournament, context={"request": request}).data
        )


class TournamentFinalsGenerateView(APIView):
    """Generate a knockout bracket from finalized pool standings."""

    def post(self, request: Request, tournament_id: str) -> Response:
        """Create and return a single-elimination finals stage.

        Raises:
            ValidationError: If pool play or qualifier counts are invalid.

        """
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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

    def post(self, request: Request, tournament_id: str) -> Response:
        """Create a reviewable final group before or after pool completion.

        Raises:
            ValidationError: If the requested group cannot be planned safely.

        """
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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

    def delete(
        self,
        request: Request,
        tournament_id: str,
        group_id: str,
    ) -> Response:
        """Delete the bracket when none of its matches has live data."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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


class TournamentDisplayConfigView(APIView):
    """Read or update the display rotation configuration."""

    def get(self, request: Request, tournament_id: str) -> Response:
        """Return the presentation configuration to a manager."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        return Response(
            TournamentDisplayConfigSerializer(tournament.display_config).data
        )

    def patch(self, request: Request, tournament_id: str) -> Response:
        """Update presentation rotation and branding fields."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        serializer = TournamentDisplayConfigSerializer(
            tournament.display_config,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        touch_tournament(tournament)
        return Response(serializer.data)


class TournamentMemberListCreateView(APIView):
    """List and grant tournament collaboration roles."""

    def get(self, request: Request, tournament_id: str) -> Response:
        """List managers and scorekeepers for a tournament."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        members = tournament.member_roles.select_related("user", "field")
        return Response(TournamentMemberSerializer(members, many=True).data)

    def post(self, request: Request, tournament_id: str) -> Response:
        """Grant one manager or field-scoped scorekeeper role."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
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
        tournament = _get_tournament(tournament_id)
        member = get_object_or_404(tournament.member_roles, pk=member_id)
        return tournament, member

    def patch(self, request: Request, tournament_id: str, member_id: int) -> Response:
        """Change role or assigned field."""
        tournament, member = self._objects(tournament_id, member_id)
        _require_manager(request, tournament)
        serializer = TournamentMemberSerializer(
            member,
            data=request.data,
            partial=True,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request: Request, tournament_id: str, member_id: int) -> Response:
        """Revoke a role without affecting result history."""
        tournament, member = self._objects(tournament_id, member_id)
        _require_manager(request, tournament)
        member.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentStandingAdjustmentListCreateView(APIView):
    """List or add audited pool-table bonuses and penalties."""

    def get(self, request: Request, tournament_id: str) -> Response:
        """List all standings adjustments for managers."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        adjustments = TournamentStandingAdjustment.objects.filter(
            entry__pool__tournament=tournament
        ).select_related("entry", "created_by")
        return Response(
            TournamentStandingAdjustmentSerializer(adjustments, many=True).data
        )

    def post(self, request: Request, tournament_id: str) -> Response:
        """Apply a reasoned points adjustment to one pool entry."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        serializer = TournamentStandingAdjustmentSerializer(
            data=request.data,
            context={"request": request, "tournament": tournament},
        )
        serializer.is_valid(raise_exception=True)
        adjustment = serializer.save(created_by=request.user)
        touch_tournament(tournament)
        return Response(
            TournamentStandingAdjustmentSerializer(adjustment).data,
            status=status.HTTP_201_CREATED,
        )


class TournamentStandingAdjustmentDetailView(APIView):
    """Remove an incorrect standings adjustment."""

    def delete(
        self, request: Request, tournament_id: str, adjustment_id: str
    ) -> Response:
        """Remove an adjustment and refresh public standings."""
        tournament = _get_tournament(tournament_id)
        _require_manager(request, tournament)
        adjustment = get_object_or_404(
            TournamentStandingAdjustment,
            id_uuid=adjustment_id,
            entry__pool__tournament=tournament,
        )
        adjustment.delete()
        touch_tournament(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


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


def _sync_advanced_winner(match: TournamentMatch) -> None:
    if not match.next_match or not match.winner_to_side:
        return
    destination = match.next_match
    replacement = match.winner if match.status == TournamentMatch.Status.FINAL else None
    if match.winner_to_side == TournamentMatch.DestinationSide.HOME:
        destination.home_team = replacement
        update_field = "home_team"
    else:
        destination.away_team = replacement
        update_field = "away_team"
    destination.save(update_fields=[update_field, "updated_at"])


def _downstream_result_locked(
    match: TournamentMatch, winner: TournamentTeam | None
) -> bool:
    """Return whether changing an advanced team would invalidate played data."""
    if not match.next_match or match.winner_id == getattr(winner, "pk", None):
        return False
    destination = match.next_match
    return (
        destination.status != TournamentMatch.Status.SCHEDULED
        or destination.home_score is not None
        or destination.away_score is not None
    )


class TournamentMatchResultView(APIView):
    """Enter, finalize, reopen, or correct one match result."""

    @transaction.atomic
    def patch(self, request: Request, match_id: str) -> Response:
        """Apply one optimistic-lock result update and record its history.

        Raises:
            PermissionDenied: If the viewer cannot score the selected match.
            Conflict: If another device has already changed the result.
            ValidationError: If scores or a knockout winner are invalid.

        """
        match = get_object_or_404(
            TournamentMatch.objects.select_for_update().select_related(
                "tournament", "stage", "field", "home_team", "away_team", "next_match"
            ),
            id_uuid=match_id,
        )
        _require_authentication(request)
        if not can_score_match(request.user, match):
            raise PermissionDenied("You cannot score this match.")
        serializer = TournamentResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["expected_revision"] != match.revision:
            raise Conflict()
        if match.home_team is None or match.away_team is None:
            return Response(
                {"detail": "Both teams must be known before entering a result."},
                status=status.HTTP_409_CONFLICT,
            )

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
        if _downstream_result_locked(match, target_winner):
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

        _sync_advanced_winner(match)

        if match.stage.kind == TournamentStage.Kind.POOL:
            try:
                resolve_final_group_qualifiers(match.tournament)
            except FinalGroupError as exc:
                raise Conflict(str(exc)) from exc

        touch_tournament(match.tournament)
        return Response({
            "id_uuid": str(match.id_uuid),
            "home_score": match.home_score,
            "away_score": match.away_score,
            "status": match.status,
            "winner_id": str(match.winner_id) if match.winner_id else None,
            "revision": match.revision,
        })


def _referee_match(request: Request, match_id: str, *, lock: bool) -> TournamentMatch:
    _require_authentication(request)
    queryset = TournamentMatch.objects.select_related(
        "tournament", "field", "home_team", "away_team"
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    match = get_object_or_404(queryset, id_uuid=match_id)
    if not can_score_match(request.user, match):
        raise PermissionDenied("You cannot score this match.")
    return match


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

    def get(self, request: Request, match_id: str) -> Response:
        """Return a match only when the viewer may score its field."""
        match = _referee_match(request, match_id, lock=False)
        return Response(build_referee_tracker_state(match))


class TournamentRefereeReadyView(APIView):
    """Record that one fixture's field is ready for the central start."""

    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Apply an idempotent, revision-checked readiness command."""
        serializer = TournamentRefereeReadySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match = _referee_match(request, match_id, lock=True)
        if match.field_ready_at is not None:
            return Response(build_referee_tracker_state(match))
        expected_revision = serializer.validated_data["expected_revision"]
        if expected_revision != match.revision:
            return _referee_conflict(
                match,
                REFEREE_REVISION_CONFLICT_DETAIL,
            )
        try:
            mark_field_ready(match, actor=request.user)
        except RefereeTrackerError as exc:
            return _referee_conflict(match, str(exc))
        touch_tournament(match.tournament)
        return Response(build_referee_tracker_state(match))


class TournamentRefereeGoalView(APIView):
    """Record one home or away goal from the field referee."""

    @transaction.atomic
    def post(self, request: Request, match_id: str) -> Response:
        """Increment exactly one score under the aggregate lock."""
        serializer = TournamentRefereeGoalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match = _referee_match(request, match_id, lock=True)
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
                actor=request.user,
            )
        except RefereeTrackerError as exc:
            return _referee_conflict(match, str(exc))
        touch_tournament(match.tournament)
        return Response(build_referee_tracker_state(match))
