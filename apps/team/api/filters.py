"""Search the team catalog without filtering a join for every imported team."""

from __future__ import annotations

from django.db.models import Q, QuerySet
from rest_framework.filters import SearchFilter
from rest_framework.request import Request

from apps.club.models import Club
from apps.team.models import Team


class TeamSearchFilter(SearchFilter):
    """Preserve DRF's AND-of-terms search while matching club names once per club."""

    def filter_queryset(
        self, request: Request, queryset: QuerySet[Team], view: object
    ) -> QuerySet[Team]:
        """Filter teams by their own name or a matching club's identity."""
        for term in self.get_search_terms(request):
            matching_clubs = Club.objects.filter(name__icontains=term).values("pk")
            queryset = queryset.filter(
                Q(name__icontains=term) | Q(club_id__in=matching_clubs)
            )
        return queryset
