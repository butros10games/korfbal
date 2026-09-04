"""Validation contracts for tournament management APIs."""

from __future__ import annotations

from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils.text import slugify
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.tournament.api.permissions import can_manage_tournament
from apps.tournament.models import (
    Tournament,
    TournamentDisplayConfig,
    TournamentField,
    TournamentFinalGroup,
    TournamentMatch,
    TournamentMember,
    TournamentPoolEntry,
    TournamentStandingAdjustment,
    TournamentTeam,
)


SUPPORTED_TIEBREAKERS = {
    "points",
    "goal_difference",
    "goals_for",
    "head_to_head",
    "seed",
    "name",
}
MAX_IMPORTED_MATCHES = 1000
MAX_SUBSTITUTION_MATCHES = 100
FINAL_GROUP_SEMIFINAL_COUNT = 2


def unique_tournament_slug(name: str) -> str:
    """Build a readable slug and append a suffix only when necessary."""
    base = slugify(name)[:190] or "toernooi"
    candidate = base
    counter = 2
    while Tournament.objects.filter(slug=candidate).exists():
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


class TournamentSerializer(serializers.ModelSerializer):
    """Manage tournament identity, rules, and lifecycle settings."""

    can_manage = serializers.SerializerMethodField()
    team_count = serializers.IntegerField(read_only=True, default=0)
    field_count = serializers.IntegerField(read_only=True, default=0)
    match_count = serializers.IntegerField(read_only=True, default=0)
    organizer_club_name = serializers.CharField(
        source="organizer_club.name", read_only=True, allow_null=True
    )

    class Meta:
        """Expose editable rules alongside derived management metadata."""

        model = Tournament
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "name",
            "slug",
            "location",
            "timezone",
            "starts_at",
            "ends_at",
            "status",
            "visibility",
            "organizer_club",
            "organizer_club_name",
            "win_points",
            "draw_points",
            "loss_points",
            "tiebreakers",
            "match_duration_minutes",
            "changeover_minutes",
            "minimum_rest_minutes",
            "live_revision",
            "can_manage",
            "team_count",
            "field_count",
            "match_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields: ClassVar[list[str]] = [
            "id_uuid",
            "slug",
            "status",
            "live_revision",
            "can_manage",
            "team_count",
            "field_count",
            "match_count",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Validate the event range and supported standings rules.

        Raises:
            PermissionDenied: If the viewer cannot organize for the selected club.
            serializers.ValidationError: If dates or rules are invalid.

        """
        instance = self.instance if isinstance(self.instance, Tournament) else None
        starts_at = attrs.get("starts_at") or getattr(instance, "starts_at", None)
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at and ends_at and ends_at < starts_at:
            raise serializers.ValidationError({
                "ends_at": "End time must be on or after the start time."
            })
        rules = attrs.get("tiebreakers")
        if rules is not None:
            if not isinstance(rules, list) or not rules:
                raise serializers.ValidationError({
                    "tiebreakers": "Provide at least one standings rule."
                })
            unsupported = [rule for rule in rules if rule not in SUPPORTED_TIEBREAKERS]
            if unsupported:
                raise serializers.ValidationError({
                    "tiebreakers": f"Unsupported rules: {', '.join(unsupported)}"
                })
        organizer_club = attrs.get(
            "organizer_club", getattr(instance, "organizer_club", None)
        )
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if organizer_club and not (
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or organizer_club.admin.filter(user=user).exists()
        ):
            raise PermissionDenied(
                "Only a club administrator can organize a tournament for this club."
            )
        return attrs

    def validate_timezone(self, value: str) -> str:
        """Require an installed IANA timezone before schedule operations use it.

        Raises:
            serializers.ValidationError: If the timezone cannot be loaded.

        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise serializers.ValidationError(
                "Select a valid IANA timezone, such as Europe/Amsterdam."
            ) from exc
        return value

    def create(self, validated_data: dict[str, Any]) -> Tournament:
        """Create an owned tournament with a collision-safe public slug."""
        request = self.context["request"]
        return Tournament.objects.create(
            **validated_data,
            owner=request.user,
            slug=unique_tournament_slug(validated_data["name"]),
        )

    def get_can_manage(self, obj: Tournament) -> bool:
        """Return the current viewer's structural-management capability."""
        request = self.context.get("request")
        return bool(request and can_manage_tournament(request.user, obj))


class TournamentTeamSerializer(serializers.ModelSerializer):
    """Manage event-scoped custom teams."""

    linked_team_name = serializers.CharField(
        source="linked_team.name", read_only=True, allow_null=True
    )

    class Meta:
        """Expose custom-team identity and tournament state."""

        model = TournamentTeam
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "name",
            "short_name",
            "affiliation",
            "linked_team",
            "linked_team_name",
            "seed",
            "sort_order",
            "color",
            "checked_in",
            "withdrawn",
            "created_at",
        ]
        read_only_fields: ClassVar[list[str]] = ["id_uuid", "created_at"]

    def validate_name(self, value: str) -> str:
        """Keep custom team names case-insensitively unique.

        Raises:
            serializers.ValidationError: If a duplicate name exists.

        """
        tournament = self.context["tournament"]
        duplicate = TournamentTeam.objects.filter(
            tournament=tournament,
            name__iexact=value.strip(),
        )
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError("A team with this name already exists.")
        return value.strip()


class TournamentFieldSerializer(serializers.ModelSerializer):
    """Manage free-form court and field labels."""

    class Meta:
        """Expose editable field labels and ordering."""

        model = TournamentField
        fields: ClassVar[list[str]] = ["id_uuid", "label", "sort_order", "active"]
        read_only_fields: ClassVar[list[str]] = ["id_uuid"]

    def validate_label(self, value: str) -> str:
        """Keep field labels case-insensitively unique.

        Raises:
            serializers.ValidationError: If a duplicate label exists.

        """
        tournament = self.context["tournament"]
        duplicate = TournamentField.objects.filter(
            tournament=tournament,
            label__iexact=value.strip(),
        )
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError("A field with this label already exists.")
        return value.strip()


class TournamentDisplayConfigSerializer(serializers.ModelSerializer):
    """Manage presentation screen rotation and branding."""

    sponsors = serializers.ListField(
        child=serializers.CharField(max_length=120, trim_whitespace=True),
        allow_empty=True,
        max_length=6,
        required=False,
    )

    class Meta:
        """Expose presentation-only configuration."""

        model = TournamentDisplayConfig
        fields: ClassVar[list[str]] = [
            "rotation_seconds",
            "show_live",
            "show_standings",
            "show_upcoming",
            "show_recent",
            "show_sponsors",
            "accent_color",
            "announcement",
            "sponsors",
        ]


class TournamentMemberSerializer(serializers.ModelSerializer):
    """Manage organizer and field-scoped scorekeeper access."""

    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        """Expose explicit roles without leaking unrelated account fields."""

        model = TournamentMember
        fields: ClassVar[list[str]] = ["id", "user", "username", "role", "field"]
        read_only_fields: ClassVar[list[str]] = ["id", "username"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Require a same-tournament field only for scorekeepers.

        Raises:
            serializers.ValidationError: If the selected field or role is invalid.

        """
        tournament = self.context["tournament"]
        role = attrs.get("role", getattr(self.instance, "role", None))
        field = attrs.get("field", getattr(self.instance, "field", None))
        if field and field.tournament_id != tournament.pk:
            raise serializers.ValidationError({"field": "Select a tournament field."})
        if role == TournamentMember.Role.MANAGER and field:
            raise serializers.ValidationError({
                "field": "Managers cannot be field-scoped."
            })
        return attrs


class TournamentStandingAdjustmentSerializer(serializers.ModelSerializer):
    """Validate audited pool-table bonuses and penalties."""

    class Meta:
        """Expose adjustment identity, value, and audit metadata."""

        model = TournamentStandingAdjustment
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "entry",
            "points",
            "reason",
            "created_at",
        ]
        read_only_fields: ClassVar[list[str]] = ["id_uuid", "created_at"]

    def validate_entry(self, value: TournamentPoolEntry) -> TournamentPoolEntry:
        """Reject entries outside the managed tournament.

        Raises:
            serializers.ValidationError: If the pool entry belongs elsewhere.

        """
        if value.pool.tournament_id != self.context["tournament"].pk:
            raise serializers.ValidationError("Select a pool entry in this tournament.")
        return value


class GenerationRequestSerializer(serializers.Serializer):
    """Validate parameters used by preview and apply generation."""

    pool_count = serializers.IntegerField(min_value=1, max_value=26)
    strategy = serializers.ChoiceField(
        choices=("snake", "seeded", "random"), default="snake"
    )
    random_seed = serializers.IntegerField(default=1)
    legs = serializers.ChoiceField(choices=(1, 2), default=1)
    starts_at = serializers.DateTimeField(required=False)
    duration_minutes = serializers.IntegerField(
        min_value=1, max_value=240, required=False
    )
    changeover_minutes = serializers.IntegerField(
        min_value=0, max_value=120, required=False
    )
    minimum_rest_minutes = serializers.IntegerField(
        min_value=0, max_value=240, required=False
    )


class PoolGenerationRequestSerializer(serializers.Serializer):
    """Validate a pool-only generation action."""

    pool_count = serializers.IntegerField(min_value=1, max_value=26)
    strategy = serializers.ChoiceField(
        choices=("snake", "seeded", "random"), default="snake"
    )
    random_seed = serializers.IntegerField(default=1)


class MatchGenerationRequestSerializer(serializers.Serializer):
    """Validate scheduling options for already reviewed pools."""

    legs = serializers.ChoiceField(choices=(1, 2), default=1)
    starts_at = serializers.DateTimeField(required=False)
    duration_minutes = serializers.IntegerField(
        min_value=1, max_value=240, required=False
    )
    changeover_minutes = serializers.IntegerField(
        min_value=0, max_value=120, required=False
    )
    minimum_rest_minutes = serializers.IntegerField(
        min_value=0, max_value=240, required=False
    )


class TournamentPoolWriteSerializer(serializers.Serializer):
    """Validate one manually composed tournament pool."""

    name = serializers.CharField(max_length=80)
    sort_order = serializers.IntegerField(min_value=0, max_value=32767, required=False)
    assigned_field_id = serializers.UUIDField(required=False, allow_null=True)
    team_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=2,
        max_length=100,
    )


class TournamentMatchWriteSerializer(serializers.Serializer):
    """Validate one manually scheduled pool match."""

    pool_id = serializers.UUIDField()
    home_team_id = serializers.UUIDField()
    away_team_id = serializers.UUIDField()
    field_id = serializers.UUIDField()
    date = serializers.DateField()
    start_time = serializers.TimeField()
    duration_minutes = serializers.IntegerField(min_value=1, max_value=240)
    round_number = serializers.IntegerField(min_value=1, max_value=999)


class TournamentMatchSubstitutionSerializer(serializers.Serializer):
    """Select a guest team for one absent team's pool match."""

    match_id = serializers.UUIDField()
    substitute_team_id = serializers.UUIDField()


class TournamentTeamSubstitutionSerializer(serializers.Serializer):
    """Validate an atomic replacement plan for an absent team."""

    replacements = TournamentMatchSubstitutionSerializer(
        many=True,
        required=False,
        default=list,
    )
    referee_replacements = TournamentMatchSubstitutionSerializer(
        many=True,
        required=False,
        default=list,
    )

    def validate(
        self,
        attrs: dict[str, Any],
    ) -> dict[str, Any]:
        """Require unique, reasonably bounded match assignments.

        Raises:
            serializers.ValidationError: If an assignment list is invalid.

        """
        if not attrs["replacements"] and not attrs["referee_replacements"]:
            raise serializers.ValidationError("Select at least one replacement.")
        for field in ("replacements", "referee_replacements"):
            value = attrs[field]
            if len(value) > MAX_SUBSTITUTION_MATCHES:
                raise serializers.ValidationError(
                    f"Select no more than {MAX_SUBSTITUTION_MATCHES} matches."
                )
            match_ids = [replacement["match_id"] for replacement in value]
            if len(set(match_ids)) != len(match_ids):
                raise serializers.ValidationError("Select each match only once.")
        return attrs


class TournamentScheduleImportRowSerializer(serializers.Serializer):
    """Validate one fixture from an existing tournament plan."""

    date = serializers.DateField()
    start_time = serializers.TimeField()
    pool_name = serializers.CharField(max_length=80)
    field_label = serializers.CharField(max_length=80)
    home_team_name = serializers.CharField(max_length=160)
    away_team_name = serializers.CharField(max_length=160)
    duration_minutes = serializers.IntegerField(
        min_value=1,
        max_value=240,
        required=False,
    )


class TournamentScheduleImportSerializer(serializers.Serializer):
    """Validate a bounded existing schedule import."""

    rows = TournamentScheduleImportRowSerializer(
        many=True,
        allow_empty=False,
    )

    def validate_rows(
        self,
        value: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep pasted schedules at a practical request size.

        Raises:
            serializers.ValidationError: If the schedule exceeds 1,000 matches.

        """
        if len(value) > MAX_IMPORTED_MATCHES:
            raise serializers.ValidationError("Import at most 1,000 matches at a time.")
        return value


class FinalsGenerationSerializer(serializers.Serializer):
    """Validate automatic knockout generation settings."""

    qualifiers_per_pool = serializers.IntegerField(min_value=1, max_value=8)
    starts_at = serializers.DateTimeField(required=False)


class FinalGroupMatchPlanSerializer(serializers.Serializer):
    """Validate the field, local start, and duration of one bracket match."""

    date = serializers.DateField()
    start_time = serializers.TimeField()
    field_id = serializers.UUIDField()
    duration_minutes = serializers.IntegerField(min_value=1, max_value=240)


class TournamentFinalGroupWriteSerializer(serializers.Serializer):
    """Validate one preplanned four-team final group."""

    name = serializers.CharField(max_length=90)
    format = serializers.ChoiceField(choices=TournamentFinalGroup.Format.choices)
    pool_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=2,
        max_length=3,
    )
    semifinals = FinalGroupMatchPlanSerializer(
        many=True,
        allow_empty=False,
    )
    final = FinalGroupMatchPlanSerializer()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Match the selected format to its required number of pools.

        Raises:
            serializers.ValidationError: If pool selections do not fit the format.

        """
        expected = {
            TournamentFinalGroup.Format.THREE_POOL_WILDCARD: 3,
            TournamentFinalGroup.Format.TWO_POOL_CROSS: 2,
        }[attrs["format"]]
        if len(attrs["pool_ids"]) != expected:
            raise serializers.ValidationError({
                "pool_ids": f"Select exactly {expected} pools for this format."
            })
        if len(set(attrs["pool_ids"])) != len(attrs["pool_ids"]):
            raise serializers.ValidationError({
                "pool_ids": "Select each pool only once."
            })
        if len(attrs["semifinals"]) != FINAL_GROUP_SEMIFINAL_COUNT:
            raise serializers.ValidationError({
                "semifinals": "Plan exactly two semifinals."
            })
        return attrs


class TournamentResultSerializer(serializers.Serializer):
    """Validate a concurrency-safe direct result update."""

    home_score = serializers.IntegerField(min_value=0, max_value=999, allow_null=True)
    away_score = serializers.IntegerField(min_value=0, max_value=999, allow_null=True)
    status = serializers.ChoiceField(choices=TournamentMatch.Status.choices)
    expected_revision = serializers.IntegerField(min_value=0)
    winner_id = serializers.UUIDField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Require complete scores when finalizing a result.

        Raises:
            serializers.ValidationError: If a final score is incomplete.

        """
        if attrs["status"] == TournamentMatch.Status.FINAL and (
            attrs["home_score"] is None or attrs["away_score"] is None
        ):
            raise serializers.ValidationError(
                "Both scores are required before finalizing a match."
            )
        return attrs


class TournamentRefereeReadySerializer(serializers.Serializer):
    """Validate a revision-safe field-readiness command."""

    expected_revision = serializers.IntegerField(min_value=0)


class TournamentRefereeAssignmentSerializer(serializers.Serializer):
    """Validate a manager's team-duty assignment or claim reset."""

    team_id = serializers.UUIDField(required=False, allow_null=True)
    reset_claim = serializers.BooleanField(default=False)


class TournamentRefereeClaimSerializer(serializers.Serializer):
    """Validate one account-free referee identity claim."""

    name = serializers.CharField(
        max_length=150,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    player_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Require exactly one free-form name or roster player.

        Raises:
            serializers.ValidationError: If both or neither identity is supplied.

        """
        has_name = bool(attrs.get("name"))
        has_player = attrs.get("player_id") is not None
        if has_name == has_player:
            raise serializers.ValidationError(
                "Vul je naam in of kies jezelf uit de spelerslijst."
            )
        return attrs


class TournamentRefereeGoalSerializer(serializers.Serializer):
    """Validate a single referee goal command."""

    side = serializers.ChoiceField(choices=("home", "away"))
    expected_revision = serializers.IntegerField(min_value=0)


class TournamentRefereeEventDeleteSerializer(serializers.Serializer):
    """Validate removal of the exact event still shown to the referee."""

    event_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField(min_value=0)
