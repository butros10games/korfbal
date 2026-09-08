"""Paginated relative team-strength scores with explicit sample sizes."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.competition.services.ratings import team_ratings
from apps.kwt_common.api.pagination import StandardResultsSetPagination


class RatingFilters(serializers.Serializer):
    """Require a season rather than silently blending different rosters."""

    season = serializers.UUIDField()
    sport = serializers.CharField(required=False, max_length=80)
    club = serializers.IntegerField(required=False, min_value=1)


class TeamRatingSerializer(serializers.Serializer):
    """Expose relative scores, comparison scope and provisional sample size."""

    id = serializers.IntegerField()
    external_id = serializers.CharField()
    name = serializers.CharField()
    sport = serializers.CharField()
    club_id = serializers.IntegerField()
    rating = serializers.FloatField()
    games = serializers.IntegerField()
    provisional = serializers.BooleanField()
    comparison_group = serializers.CharField()
    baseline = serializers.FloatField(required=False)
    change = serializers.FloatField(required=False)
    original_knkv_points = serializers.CharField(required=False, allow_null=True)
    allocation = serializers.IntegerField(required=False)
    category = serializers.CharField(required=False)
    class_code = serializers.CharField(required=False)
    class_level = serializers.IntegerField(required=False, allow_null=True)
    discipline = serializers.CharField(required=False)
    phase = serializers.CharField(required=False)
    gender = serializers.CharField(required=False)


class RatingPageSerializer(serializers.Serializer):
    """Describe the bounded rating result and calculation version."""

    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    model = serializers.CharField()
    computed_at = serializers.DateTimeField()
    metadata = serializers.JSONField(required=False)
    results = TeamRatingSerializer(many=True)


class RatingsView(APIView):
    """Read local season ratings; no provider requests or mutations occur here."""

    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(parameters=[RatingFilters], responses=RatingPageSerializer)
    def get(self, request: Request) -> Response:
        """Filter and paginate scores computed from the full comparison corpus."""
        filters = RatingFilters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        values = filters.validated_data
        result = team_ratings(values["season"])
        rows = result["results"]
        for parameter, field in (("sport", "sport"), ("club", "club_id")):
            if parameter in values:
                rows = [row for row in rows if row[field] == values[parameter]]
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response(page)
        response.data.update(model=result["model"], computed_at=result["computed_at"])
        if "metadata" in result:
            response.data["metadata"] = result["metadata"]
        return response
