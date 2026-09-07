"""Public competition fields only; no upstream session or player records."""

from rest_framework import serializers

from apps.competition.models import (
    Club,
    Match,
    Pool,
    PoolEntry,
    SyncResource,
    Team,
    TeamGroup,
)
from apps.schedule.models import Season


class CompetitionClubSerializer(serializers.ModelSerializer):
    """Expose catalogue club identity."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = Club
        fields = ("id", "external_id", "name", "city", "local_club")


class CompetitionTeamSerializer(serializers.ModelSerializer):
    """Expose season-specific teams and their clubs."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = Team
        fields = ("id", "external_id", "season", "club", "name", "sport", "group")


class CompetitionTeamGroupSerializer(serializers.ModelSerializer):
    """Expose one application team with its distinct provider variants."""

    variants = CompetitionTeamSerializer(many=True, read_only=True)

    class Meta:
        """Declare the unified team representation."""

        model = TeamGroup
        fields = ("id", "season", "club", "name", "local_team", "variants")


class CompetitionPoolSerializer(serializers.ModelSerializer):
    """Expose poule labels and source coverage metadata."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = Pool
        fields = (
            "id",
            "external_id",
            "season",
            "name",
            "class_name",
            "sport",
            "local_pool",
            "standings_synced_at",
            "results_filtered",
        )


class CompetitionMatchSerializer(serializers.ModelSerializer):
    """Expose normalized fixtures, scores and observation timestamps."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = Match
        fields = (
            "id",
            "external_id",
            "season",
            "pool",
            "home_team",
            "away_team",
            "starts_at",
            "status",
            "home_score",
            "away_score",
            "automatic_result",
            "result_observed_at",
            "results_checked_at",
            "local_match",
            "updated_at",
        )


class CompetitionPoolEntrySerializer(serializers.ModelSerializer):
    """Return official standing values with the team label in one query."""

    team = CompetitionTeamSerializer()

    class Meta:
        """Declare storage or serialization metadata."""

        model = PoolEntry
        fields = ("team", "standing")


class CompetitionResourceSerializer(serializers.ModelSerializer):
    """Expose partial discovery and stale or failing feeds honestly."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = SyncResource
        fields = (
            "id",
            "season",
            "kind",
            "source_id",
            "fetched_at",
            "next_sync_at",
            "failures",
            "last_error",
        )


class CompetitionSeasonSerializer(serializers.ModelSerializer):
    """Expose season IDs and date boundaries for filter controls."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = Season
        fields = ("id_uuid", "name", "start_date", "end_date")


class CompetitionFilters(serializers.Serializer):
    """Validate filters before passing user input into database lookups."""

    local_club = serializers.UUIDField(required=False)
    local_team = serializers.UUIDField(required=False)
    local_pool = serializers.UUIDField(required=False)
    local_match = serializers.UUIDField(required=False)
    season = serializers.UUIDField(required=False)
    club = serializers.IntegerField(required=False, min_value=1)
    team = serializers.IntegerField(required=False, min_value=1)
    team_group = serializers.IntegerField(required=False, min_value=1)
    pool = serializers.IntegerField(required=False, min_value=1)
    sport = serializers.CharField(required=False, max_length=80)
    status = serializers.CharField(required=False, max_length=40)
    date_from = serializers.DateTimeField(required=False)
    date_to = serializers.DateTimeField(required=False)
