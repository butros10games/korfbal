"""Staff-facing season management endpoints."""

from __future__ import annotations

from django.db.models import Count, QuerySet
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.schedule.models import Season, SeasonPool

from .serializers import SeasonPoolSerializer, SeasonSerializer


class SeasonViewSet(viewsets.ModelViewSet):
    """List and edit seasons for authenticated staff users."""

    serializer_class = SeasonSerializer
    permission_classes = (permissions.IsAdminUser,)
    lookup_field = "id_uuid"
    http_method_names = ("get", "post", "patch", "head", "options")

    def get_queryset(self) -> QuerySet[Season]:
        """Return newest seasons first with their match totals."""
        return Season.objects.annotate(
            match_count=Count("matches", distinct=True),
            pool_count=Count("pools", distinct=True),
        ).order_by("-start_date", "-end_date")

    @action(
        detail=False,
        methods=("GET",),
        url_path="access",
        permission_classes=(permissions.AllowAny,),
    )
    def access(self, request: Request) -> Response:
        """Expose only the capability needed to reveal the editor entry point."""
        user = request.user
        can_manage = bool(
            user
            and user.is_authenticated
            and (
                getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
            )
        )
        return Response({"can_manage": can_manage})


class SeasonPoolViewSet(viewsets.ModelViewSet):
    """List and edit team pools inside a season."""

    serializer_class = SeasonPoolSerializer
    permission_classes = (permissions.IsAdminUser,)
    lookup_field = "id_uuid"
    http_method_names = ("get", "post", "patch", "head", "options")

    def get_queryset(self) -> QuerySet[SeasonPool]:
        """Return pools for an optional season filter with teams and match totals."""
        queryset = (
            SeasonPool.objects
            .select_related("season")
            .prefetch_related("teams__club")
            .annotate(match_count=Count("matches", distinct=True))
            .order_by("name")
        )
        season_id = self.request.query_params.get("season")
        if season_id:
            queryset = queryset.filter(season_id=season_id)
        return queryset
