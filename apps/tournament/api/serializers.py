"""Validation contracts for tournament management APIs."""

from __future__ import annotations

from typing import Any, ClassVar

from django.utils.text import slugify
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.tournament.api.permissions import can_manage_tournament
from apps.tournament.models import (
    Tournament,
    TournamentDisplayConfig,
    TournamentField,
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

    class Meta:
        """Expose presentation-only configuration."""

        model = TournamentDisplayConfig
        fields: ClassVar[list[str]] = [
            "rotation_seconds",
            "show_live",
            "show_standings",
            "show_upcoming",
            "show_recent",
            "accent_color",
            "announcement",
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
    team_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
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


class TournamentRefereeGoalSerializer(serializers.Serializer):
    """Validate a single referee goal command."""

    side = serializers.ChoiceField(choices=("home", "away"))
    expected_revision = serializers.IntegerField(min_value=0)
