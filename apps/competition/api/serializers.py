"""Public competition fields only; no upstream session or player records."""

from rest_framework import serializers

from apps.competition.domain.classification import designation
from apps.competition.models import (
    Allocation,
    Club,
    CompetitionEdition,
    Match,
    Pool,
    PoolEntry,
    SyncResource,
    Team,
    TeamGroup,
)
from apps.competition.services.classification import pool_classification
from apps.competition.services.seasons import SeasonResolver
from apps.schedule.models import Season


class CompetitionClubSerializer(serializers.ModelSerializer):
    """Expose catalogue club identity."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = Club
        fields = ("id", "external_id", "name", "city", "local_club")


class NativeSeasonSerializer(serializers.ModelSerializer):
    """Expose the playing season, retaining fetch scope only in source storage."""

    _season_resolver: SeasonResolver | None = None
    season = serializers.SerializerMethodField()

    def get_season(self, obj: Team | Pool | Match) -> str | None:
        """Reuse one mapping index for nested catalogue serialization."""
        if self._season_resolver is None:
            self._season_resolver = SeasonResolver()
        resolver = self._season_resolver
        sport = obj.home_team.sport if isinstance(obj, Match) else obj.sport
        value = resolver.resolve(obj.season_id, sport)
        return str(value) if value is not None else None


class CompetitionTeamSerializer(NativeSeasonSerializer):
    """Expose season-specific teams and their clubs."""

    designation = serializers.SerializerMethodField()
    local_team = serializers.UUIDField(
        source="group.local_team_id", read_only=True, allow_null=True
    )

    def get_designation(self, obj: Team) -> dict:
        """Keep J numbering independent of age."""
        return designation(obj.name, obj.season.start_date.year)

    class Meta:
        """Declare storage or serialization metadata."""

        model = Team
        fields = (
            "id",
            "external_id",
            "season",
            "club",
            "name",
            "sport",
            "group",
            "designation",
            "local_team",
        )


class CompetitionTeamGroupSerializer(serializers.ModelSerializer):
    """Expose one application team with its distinct provider variants."""

    variants = CompetitionTeamSerializer(many=True, read_only=True)

    class Meta:
        """Declare the unified team representation."""

        model = TeamGroup
        fields = ("id", "season", "club", "name", "local_team", "variants")


class CompetitionPoolSerializer(NativeSeasonSerializer):
    """Expose poule labels and source coverage metadata."""

    classification = serializers.SerializerMethodField()
    teams = CompetitionTeamSerializer(source="member_teams", many=True, read_only=True)

    def get_classification(self, obj: Pool) -> dict | None:
        """Use the same official classification as native views."""
        return pool_classification(obj)

    class Meta:
        """Declare storage or serialization metadata."""

        model = Pool
        fields = (
            "id",
            "external_id",
            "season",
            "name",
            "class_name",
            "classification",
            "teams",
            "sport",
            "local_pool",
            "standings_synced_at",
            "results_filtered",
        )


class CompetitionMatchSerializer(NativeSeasonSerializer):
    """Expose normalized fixtures, scores and observation timestamps."""

    class Meta:
        """Declare storage or serialization metadata."""

        model = Match
        fields = (
            "private_lineup_counts",
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


class CompetitionEditionSerializer(serializers.ModelSerializer):
    """Expose the playing context underneath the annual season."""

    class Meta:
        """Keep season and competition edition separate."""

        model = CompetitionEdition
        fields = ("id", "discipline", "phase", "gender")


class CompetitionSeasonSerializer(serializers.ModelSerializer):
    """Expose season IDs and date boundaries for filter controls."""

    editions = CompetitionEditionSerializer(
        source="competitionedition_set", many=True, read_only=True
    )

    class Meta:
        """Declare storage or serialization metadata."""

        model = Season
        fields = ("id_uuid", "name", "start_date", "end_date", "editions")


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
    discipline = serializers.ChoiceField(
        choices=("indoor", "outdoor", "unknown"), required=False
    )
    phase = serializers.ChoiceField(
        choices=("autumn", "spring", "indoor", "full_season", "unknown"), required=False
    )
    category = serializers.ChoiceField(
        choices=("top", "a", "b", "unknown"), required=False
    )
    gender = serializers.ChoiceField(
        choices=("mixed", "women", "unknown"), required=False
    )
    age_group = serializers.CharField(required=False, max_length=20)
    class_code = serializers.CharField(required=False, max_length=40)
    mapping_status = serializers.ChoiceField(
        choices=("mapped", "partial", "unresolved", "conflict"), required=False
    )
    status = serializers.CharField(required=False, max_length=40)
    date_from = serializers.DateTimeField(required=False)
    date_to = serializers.DateTimeField(required=False)


class AllocationSerializer(serializers.ModelSerializer):
    """Read original published aggregate metadata, not inferred ages or ratings."""

    published_on = serializers.DateField(source="source.published_on")
    source_label = serializers.CharField(source="source.label")

    class Meta:
        """Expose aggregate source values and link coverage."""

        model = Allocation
        fields = (
            "id",
            "source_label",
            "published_on",
            "section",
            "pool_name",
            "team_name",
            "city",
            "match_day",
            "average_age",
            "knkv_points",
            "classification",
            "entry",
            "link_status",
        )
