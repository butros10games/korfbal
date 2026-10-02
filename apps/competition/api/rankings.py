"""Public club-team rankings from the cross-season Elo replay."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.competition.domain.classification import CHOICES
from apps.competition.domain.team_elo import MODEL_VERSION
from apps.competition.queries.rankings import ACTIVE_DAYS, ranking_row, rankings
from apps.kwt_common.api.pagination import StandardResultsSetPagination


def choices(field: str) -> list[str]:
    """Offer the classification's known values, excluding unknown."""
    return sorted(CHOICES[field] - {"unknown"})


class RankingFilters(serializers.Serializer):
    """An age group is required: youth and senior teams never meet."""

    age_group = serializers.ChoiceField(choices=choices("age_group"))
    gender = serializers.ChoiceField(choices=choices("gender"), required=False)
    category = serializers.ChoiceField(choices=choices("category"), required=False)
    team_kind = serializers.ChoiceField(choices=choices("team_kind"), required=False)
    colour = serializers.ChoiceField(choices=choices("colour"), required=False)
    playing_format = serializers.ChoiceField(
        choices=choices("playing_format"), required=False
    )
    class_code = serializers.ChoiceField(choices=choices("code"), required=False)
    discipline = serializers.ChoiceField(choices=choices("discipline"), required=False)
    club = serializers.UUIDField(required=False)
    search = serializers.CharField(required=False, max_length=100)
    active_days = serializers.IntegerField(
        required=False, min_value=1, max_value=3650, default=ACTIVE_DAYS
    )


class RankingSerializer(serializers.Serializer):
    """One club team, ranked within the selection by its current rating."""

    rank = serializers.IntegerField()
    team = serializers.UUIDField()
    team_name = serializers.CharField()
    club = serializers.UUIDField()
    club_name = serializers.CharField()
    rating = serializers.FloatField()
    phase_change = serializers.FloatField()
    games = serializers.IntegerField()
    provisional = serializers.BooleanField()
    last_played_at = serializers.DateTimeField()
    comparison_group = serializers.CharField()
    discipline = serializers.CharField()
    phase = serializers.CharField()
    gender = serializers.CharField()
    category = serializers.CharField()
    age_group = serializers.CharField()
    colour = serializers.CharField()
    playing_format = serializers.CharField()
    team_kind = serializers.CharField()
    class_code = serializers.CharField()
    class_level = serializers.IntegerField(allow_null=True)


class RankingPageSerializer(serializers.Serializer):
    """A page of ranked teams and the rating model that produced them."""

    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    model = serializers.CharField()
    results = RankingSerializer(many=True)


class RankingsView(APIView):
    """Read stored ratings only; the replay runs in a background task."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(parameters=[RankingFilters], responses=RankingPageSerializer)
    def get(self, request: Request) -> Response:
        """Filter by latest classification and paginate ranked club teams."""
        filters = RankingFilters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(
            rankings(dict(filters.validated_data)), request, view=self
        )
        rows = RankingSerializer(
            [ranking_row(rating) for rating in page or []], many=True
        ).data
        response = paginator.get_paginated_response(rows)
        response.data["model"] = MODEL_VERSION
        return response
