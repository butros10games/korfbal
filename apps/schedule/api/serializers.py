"""Serializers for schedule API endpoints."""

from __future__ import annotations

from datetime import date
from typing import ClassVar, cast

from django.utils import timezone
from rest_framework import serializers

from apps.game_tracker.services.event_editor import (
    UNSET,
    CreateGoalEvent,
    CreatePauseEvent,
    CreateSubstitutionEvent,
    CreateTimeoutEvent,
    EntityId,
    UnsetValue,
    UpdateGoalEvent,
    UpdatePauseEvent,
    UpdateSubstitutionEvent,
    UpdateTimeoutEvent,
)
from apps.schedule.models import Match, Season, SeasonPool
from apps.team.api.serializers import TeamSerializer
from apps.team.models.team import Team


MIN_POOL_TEAMS = 2


class MatchSerializer(serializers.ModelSerializer):
    """Serializer for match data exposed to the frontend."""

    home_team = TeamSerializer(read_only=True)
    away_team = TeamSerializer(read_only=True)
    location = serializers.SerializerMethodField()
    competition = serializers.SerializerMethodField()
    broadcast_url = serializers.SerializerMethodField()
    season_id = serializers.UUIDField(read_only=True)
    pool_id = serializers.UUIDField(read_only=True, allow_null=True)
    pool_name = serializers.CharField(
        source="pool.name",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        """Meta options for the match serializer."""

        model = Match
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "start_time",
            "season_id",
            "pool_id",
            "pool_name",
            "home_team",
            "away_team",
            "location",
            "competition",
            "broadcast_url",
        ]
        read_only_fields: ClassVar[list[str]] = fields

    def get_location(self, obj: Match) -> str:
        """Return a friendly location for the match.

        Returns:
            str: Name of the home club, used as location label.

        """
        return obj.home_team.club.name

    def get_competition(self, obj: Match) -> str:
        """Return the competition/season label.

        Returns:
            str: Human readable season name.

        """
        return obj.season.name

    def get_broadcast_url(self, obj: Match) -> str | None:
        """Expose a placeholder for future livestream links.

        Returns:
            str | None: The livestream URL, if one is available.

        """
        return None


class MatchWriteSerializer(serializers.ModelSerializer):
    """Validate schedule-editor match create and partial-update payloads."""

    home_team_id = serializers.PrimaryKeyRelatedField(
        source="home_team",
        queryset=Team.objects.select_related("club"),
    )
    away_team_id = serializers.PrimaryKeyRelatedField(
        source="away_team",
        queryset=Team.objects.select_related("club"),
    )
    season_id = serializers.PrimaryKeyRelatedField(
        source="season",
        queryset=Season.objects.all(),
    )
    pool_id = serializers.PrimaryKeyRelatedField(
        source="pool",
        queryset=SeasonPool.objects.prefetch_related("teams"),
        required=False,
        allow_null=True,
    )

    class Meta:
        """Meta options for staff schedule writes."""

        model = Match
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "start_time",
            "season_id",
            "pool_id",
            "home_team_id",
            "away_team_id",
        ]
        read_only_fields: ClassVar[list[str]] = ["id_uuid"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Prevent a team from playing itself, including partial updates.

        Raises:
            serializers.ValidationError: If both match sides resolve to one team.

        """
        instance = self.instance if isinstance(self.instance, Match) else None
        home_team = attrs.get("home_team") or getattr(instance, "home_team", None)
        away_team = attrs.get("away_team") or getattr(instance, "away_team", None)
        if home_team is not None and home_team == away_team:
            raise serializers.ValidationError({
                "away_team_id": "Home and away team must be different."
            })

        season = attrs.get("season") or getattr(instance, "season", None)
        pool = attrs["pool"] if "pool" in attrs else getattr(instance, "pool", None)
        if isinstance(pool, SeasonPool):
            if season is None or pool.season_id != getattr(season, "id_uuid", None):
                raise serializers.ValidationError({
                    "pool_id": "Pool must belong to the selected season."
                })
            pool_team_ids = set(pool.teams.values_list("id_uuid", flat=True))
            invalid_fields = [
                field
                for field, team in (
                    ("home_team_id", home_team),
                    ("away_team_id", away_team),
                )
                if team is not None
                and getattr(team, "id_uuid", None) not in pool_team_ids
            ]
            if invalid_fields:
                raise serializers.ValidationError(
                    dict.fromkeys(
                        invalid_fields,
                        "Team must belong to the selected pool.",
                    )
                )
        return attrs

    def to_representation(self, instance: Match) -> dict[str, object]:
        """Return the same nested representation used by public match reads."""
        return dict(MatchSerializer(instance, context=self.context).data)


class SeasonSerializer(serializers.ModelSerializer):
    """Serialize seasons for the schedule editor."""

    is_current = serializers.SerializerMethodField()
    match_count = serializers.IntegerField(read_only=True, default=0)
    pool_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        """Meta options for season management."""

        model = Season
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "name",
            "start_date",
            "end_date",
            "is_current",
            "match_count",
            "pool_count",
        ]
        read_only_fields: ClassVar[list[str]] = [
            "id_uuid",
            "is_current",
            "match_count",
            "pool_count",
        ]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Require an inclusive, forward-moving season date range.

        Raises:
            serializers.ValidationError: If the end precedes the start.

        """
        instance = self.instance if isinstance(self.instance, Season) else None
        start_date = attrs.get("start_date") or getattr(instance, "start_date", None)
        end_date = attrs.get("end_date") or getattr(instance, "end_date", None)
        if (
            isinstance(start_date, date)
            and isinstance(end_date, date)
            and end_date < start_date
        ):
            raise serializers.ValidationError({
                "end_date": "End date must be on or after start date."
            })
        return attrs

    def get_is_current(self, obj: Season) -> bool:
        """Return whether today falls inside the season date range."""
        today = timezone.localdate()
        return obj.start_date <= today <= obj.end_date


class SeasonPoolSerializer(serializers.ModelSerializer):
    """Serialize a season pool and its editable team membership."""

    season_id = serializers.PrimaryKeyRelatedField(
        source="season",
        queryset=Season.objects.all(),
    )
    teams = TeamSerializer(many=True, read_only=True)
    team_ids = serializers.PrimaryKeyRelatedField(
        source="teams",
        queryset=Team.objects.select_related("club"),
        many=True,
        write_only=True,
    )
    match_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        """Expose pool membership without allowing destructive operations."""

        model = SeasonPool
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "season_id",
            "name",
            "teams",
            "team_ids",
            "match_count",
        ]
        read_only_fields: ClassVar[list[str]] = ["id_uuid", "match_count"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Keep pool identity and existing pooled matches consistent.

        Raises:
            serializers.ValidationError: If identity or membership is invalid.

        """
        instance = self.instance if isinstance(self.instance, SeasonPool) else None
        season = attrs.get("season") or getattr(instance, "season", None)
        name = attrs.get("name") or getattr(instance, "name", "")

        if instance and "season" in attrs and attrs["season"] != instance.season:
            raise serializers.ValidationError({
                "season_id": "A pool cannot be moved to another season."
            })

        duplicate = SeasonPool.objects.filter(season=season, name__iexact=name)
        if instance:
            duplicate = duplicate.exclude(pk=instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError({
                "name": "A pool with this name already exists in this season."
            })

        if "teams" in attrs:
            teams = cast(list[Team], attrs["teams"])
            selected_ids = {team.id_uuid for team in teams}
            if len(selected_ids) < MIN_POOL_TEAMS:
                raise serializers.ValidationError({
                    "team_ids": "Select at least two teams for a pool."
                })
            other_pools = SeasonPool.objects.filter(
                season=season,
                teams__in=teams,
            )
            if instance:
                other_pools = other_pools.exclude(pk=instance.pk)
            if other_pools.exists():
                raise serializers.ValidationError({
                    "team_ids": "A team can belong to only one pool per season."
                })
            if instance:
                used_ids = set(
                    Match.objects.filter(pool=instance).values_list(
                        "home_team_id", flat=True
                    )
                ) | set(
                    Match.objects.filter(pool=instance).values_list(
                        "away_team_id", flat=True
                    )
                )
                if not used_ids.issubset(selected_ids):
                    raise serializers.ValidationError({
                        "team_ids": "Teams with matches in this pool cannot be removed."
                    })

        return attrs


def _required_id(data: dict[str, object], key: str) -> EntityId:
    return cast(EntityId, data[key])


def _optional_text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _optional_integer(data: dict[str, object], key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) else None


def _integer(data: dict[str, object], key: str, *, default: int = 0) -> int:
    value = data.get(key)
    return value if isinstance(value, int) else default


def _patch[T](data: dict[str, object], key: str) -> T | UnsetValue:
    return cast(T, data[key]) if key in data else UNSET


class ShotWriteSerializer(serializers.Serializer):
    """Parse goal-event input into a typed editor command."""

    player_id = serializers.UUIDField()
    team_id = serializers.UUIDField()
    shot_type_id = serializers.UUIDField()
    match_part_id = serializers.UUIDField()
    for_team = serializers.BooleanField(required=False, default=True)
    scored = serializers.BooleanField(required=False, default=True)
    time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)

    def to_command(
        self, *, event_id: str | None = None
    ) -> CreateGoalEvent | UpdateGoalEvent:
        """Return the application command represented by validated input."""
        data = cast(dict[str, object], self.validated_data)
        if event_id is None:
            return CreateGoalEvent(
                player_id=_required_id(data, "player_id"),
                team_id=_required_id(data, "team_id"),
                shot_type_id=_required_id(data, "shot_type_id"),
                match_part_id=_required_id(data, "match_part_id"),
                time=_optional_text(data, "time"),
                minute=_optional_integer(data, "minute"),
                scored=bool(data.get("scored", True)),
                for_team=bool(data.get("for_team", True)),
            )
        return UpdateGoalEvent(
            event_id=event_id,
            player_id=_patch(data, "player_id"),
            team_id=_patch(data, "team_id"),
            shot_type_id=_patch(data, "shot_type_id"),
            match_part_id=_patch(data, "match_part_id"),
            time=_patch(data, "time"),
            minute=_patch(data, "minute"),
            scored=_patch(data, "scored"),
            for_team=_patch(data, "for_team"),
        )


class PlayerChangeWriteSerializer(serializers.Serializer):
    """Parse substitution input into a typed editor command."""

    player_in_id = serializers.UUIDField()
    player_out_id = serializers.UUIDField()
    player_group_id = serializers.UUIDField()
    match_part_id = serializers.UUIDField()
    time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)

    def to_command(
        self, *, event_id: str | None = None
    ) -> CreateSubstitutionEvent | UpdateSubstitutionEvent:
        """Return the application command represented by validated input."""
        data = cast(dict[str, object], self.validated_data)
        if event_id is None:
            return CreateSubstitutionEvent(
                player_in_id=_required_id(data, "player_in_id"),
                player_out_id=_required_id(data, "player_out_id"),
                player_group_id=_required_id(data, "player_group_id"),
                match_part_id=_required_id(data, "match_part_id"),
                time=_optional_text(data, "time"),
                minute=_optional_integer(data, "minute"),
            )
        return UpdateSubstitutionEvent(
            event_id=event_id,
            player_in_id=_patch(data, "player_in_id"),
            player_out_id=_patch(data, "player_out_id"),
            player_group_id=_patch(data, "player_group_id"),
            match_part_id=_patch(data, "match_part_id"),
            time=_patch(data, "time"),
            minute=_patch(data, "minute"),
        )


class PauseWriteSerializer(serializers.Serializer):
    """Parse pause input into a typed editor command."""

    match_part_id = serializers.UUIDField()
    start_time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)
    length_seconds = serializers.IntegerField(required=False, min_value=0)
    active = serializers.BooleanField(required=False)

    def to_command(
        self, *, event_id: str | None = None
    ) -> CreatePauseEvent | UpdatePauseEvent:
        """Return the application command represented by validated input."""
        data = cast(dict[str, object], self.validated_data)
        if event_id is None:
            return CreatePauseEvent(
                match_part_id=_required_id(data, "match_part_id"),
                start_time=_optional_text(data, "start_time"),
                minute=_optional_integer(data, "minute"),
                length_seconds=_integer(data, "length_seconds"),
                active=bool(data.get("active", False)),
            )
        return UpdatePauseEvent(
            event_id=event_id,
            match_part_id=_patch(data, "match_part_id"),
            start_time=_patch(data, "start_time"),
            minute=_patch(data, "minute"),
            length_seconds=_patch(data, "length_seconds"),
            active=_patch(data, "active"),
        )


class TimeoutWriteSerializer(serializers.Serializer):
    """Parse timeout input into a typed editor command."""

    team_id = serializers.UUIDField()
    match_part_id = serializers.UUIDField()
    start_time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)
    length_seconds = serializers.IntegerField(required=False, min_value=0)

    def to_command(
        self, *, event_id: str | None = None
    ) -> CreateTimeoutEvent | UpdateTimeoutEvent:
        """Return the application command represented by validated input."""
        data = cast(dict[str, object], self.validated_data)
        if event_id is None:
            return CreateTimeoutEvent(
                team_id=_required_id(data, "team_id"),
                match_part_id=_required_id(data, "match_part_id"),
                start_time=_optional_text(data, "start_time"),
                minute=_optional_integer(data, "minute"),
                length_seconds=_integer(data, "length_seconds"),
            )
        return UpdateTimeoutEvent(
            event_id=event_id,
            team_id=_patch(data, "team_id"),
            match_part_id=_patch(data, "match_part_id"),
            start_time=_patch(data, "start_time"),
            minute=_patch(data, "minute"),
            length_seconds=_patch(data, "length_seconds"),
        )
