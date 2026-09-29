"""ViewSets for team-related API endpoints."""

from __future__ import annotations

from django.db import models
from django.db.models import QuerySet
from rest_framework import viewsets

from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.api.params import UUID_URL_REGEX, uuid_query_value
from apps.kwt_common.api.permissions import IsStaffOrReadOnly
from apps.team.models.team import Team

from .filters import TeamSearchFilter
from .goal_song_views import TeamGoalSongAdminActions
from .overview_views import TeamOverviewActions
from .roster_views import TeamRosterActions
from .serializers import (
    TeamCatalogSerializer,
    TeamSerializer,
)


class TeamViewSet(
    TeamRosterActions,
    TeamOverviewActions,
    TeamGoalSongAdminActions,
    viewsets.ModelViewSet,
):
    """Expose team CRUD endpoints with lightweight search support."""

    queryset = (
        Team.objects
        .select_related("club")
        .order_by("club__name", "name", "id_uuid")
        .fetch_mode(models.FETCH_RAISE)
    )
    serializer_class = TeamSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = (IsStaffOrReadOnly,)
    lookup_field = "id_uuid"
    lookup_value_regex = UUID_URL_REGEX
    filter_backends = (TeamSearchFilter,)
    search_fields = ("name", "club__name")

    def get_serializer_class(self) -> type[TeamSerializer]:
        """Add club city metadata only to catalog responses."""
        return TeamCatalogSerializer if self.action == "list" else TeamSerializer

    def get_queryset(self) -> QuerySet[Team]:
        """Optionally scope the paginated catalog to one club."""
        queryset = super().get_queryset()
        if self.action == "list":
            queryset = queryset.select_related("club__competition_identity")
        if (
            self.action == "list"
            and self.request.query_params.get("followed") == "true"
        ):
            if not self.request.user.is_authenticated:
                return queryset.none()
            queryset = queryset.filter(player__user=self.request.user)
        club_id = self.request.query_params.get("club")
        if not club_id:
            return queryset
        return queryset.filter(
            club__id_uuid=uuid_query_value(club_id, parameter="club")
        )
