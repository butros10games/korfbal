"""Tournament tournaments API endpoints and helpers."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.permissions import can_manage_tournament, is_authenticated
from apps.tournament.api.serializers import (
    TournamentDisplayConfigSerializer,
    TournamentSerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentMember,
    TournamentTeam,
)
from apps.tournament.services.snapshot import build_tournament_snapshot

from .common import (
    get_tournament,
    require_authentication,
    require_manager,
    resolve_qualifiers,
)


PUBLIC_STATUSES = {
    Tournament.Status.PUBLISHED,
    Tournament.Status.LIVE,
    Tournament.Status.FINISHED,
}


def _public_access_allowed(request: Request, tournament: Tournament) -> bool:
    if can_manage_tournament(request.user, tournament):
        return True
    if tournament.status not in PUBLIC_STATUSES:
        return False
    if tournament.visibility == Tournament.Visibility.PUBLIC:
        return True
    return request.query_params.get("token") == str(tournament.display_token)


def _tournament_count(queryset: QuerySet) -> Coalesce:
    """Count one relation without multiplying teams, fields and matches together."""
    counts = (
        queryset
        .filter(tournament_id=OuterRef("pk"))
        .order_by()
        .values("tournament_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    return Coalesce(Subquery(counts), 0)


@extend_schema_view(
    partial_update=extend_schema(
        description="Patch editable tournament fields for a manager."
    ),
)
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
                team_count=_tournament_count(TournamentTeam.objects.all()),
                field_count=_tournament_count(TournamentField.objects.all()),
                match_count=_tournament_count(TournamentMatch.objects.all()),
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
        memberships = TournamentMember.objects.filter(
            tournament_id=OuterRef("pk"), user=user
        )
        return queryset.annotate(
            viewer_is_manager=Exists(
                memberships.filter(role=TournamentMember.Role.MANAGER)
            ),
        ).filter(public | Q(owner=user) | Exists(memberships))

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Create a tournament owned by the authenticated viewer."""
        require_authentication(request)
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def update(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Replace editable tournament fields for a manager."""
        tournament = self.get_object()
        require_manager(request, tournament)
        response = super().update(request, *args, **kwargs)
        resolve_qualifiers(tournament)
        touch_tournament(tournament)
        return response


class TournamentPublicView(APIView):
    """Return the public tournament snapshot by readable slug."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        responses={200: OpenApiTypes.OBJECT},
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY)],
    )
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

    @extend_schema(
        responses={200: OpenApiTypes.OBJECT},
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY)],
    )
    def get(self, request: Request, tournament_id: str) -> Response:
        """Return the current snapshot and viewer capabilities.

        Raises:
            PermissionDenied: If the viewer lacks public or manager access.

        """
        tournament = get_tournament(tournament_id)
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


class TournamentPublishView(APIView):
    """Publish a complete generated tournament."""

    @extend_schema(request=None, responses={200: TournamentSerializer})
    def post(self, request: Request, tournament_id: str) -> Response:
        """Publish a tournament after teams and matches exist."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
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


class TournamentDisplayConfigView(APIView):
    """Read or update the display rotation configuration."""

    @extend_schema(responses={200: TournamentDisplayConfigSerializer})
    def get(self, request: Request, tournament_id: str) -> Response:
        """Return the presentation configuration to a manager."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        return Response(
            TournamentDisplayConfigSerializer(tournament.display_config).data
        )

    @extend_schema(
        request=TournamentDisplayConfigSerializer,
        responses={200: TournamentDisplayConfigSerializer},
    )
    def patch(self, request: Request, tournament_id: str) -> Response:
        """Update presentation rotation and branding fields."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentDisplayConfigSerializer(
            tournament.display_config,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        touch_tournament(tournament)
        return Response(serializer.data)
