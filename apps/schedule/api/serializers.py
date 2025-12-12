"""Serializers for schedule API endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, cast

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.player.models.player import Player
from apps.schedule.models import Match
from apps.team.api.serializers import TeamSerializer
from apps.team.models.team import Team


INVALID_MATCH_PART = "Invalid match part."
UNKNOWN_PLAYER = "Unknown player."
UNKNOWN_TEAM = "Unknown team."


class MatchSerializer(serializers.ModelSerializer):
    """Serializer for match data exposed to the frontend."""

    home_team = TeamSerializer(read_only=True)
    away_team = TeamSerializer(read_only=True)
    location = serializers.SerializerMethodField()
    competition = serializers.SerializerMethodField()
    broadcast_url = serializers.SerializerMethodField()

    class Meta:
        """Meta options for the match serializer."""

        model = Match
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "start_time",
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


def _ensure_aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_int(value: object, *, default: int = 0) -> int:
    coerced = _optional_int(value)
    return coerced if coerced is not None else default


def _resolve_event_time(
    *,
    match_part: MatchPart,
    time: str | None,
    minute: int | None,
) -> datetime:
    """Resolve an event timestamp.

    Preferred input is a full ISO datetime string (time).
    As a fallback, `minute` will be interpreted as minutes after the match part start.

    Raises:
        ValidationError: If the provided time/minute input is invalid.

    """
    if time:
        parsed = parse_datetime(time)
        if parsed is None:
            raise serializers.ValidationError({"time": "Invalid datetime."})
        return _ensure_aware(parsed)

    if minute is None:
        raise serializers.ValidationError({
            "time": "Provide either 'time' (ISO datetime) or 'minute'."
        })

    if minute < 0:
        raise serializers.ValidationError({"minute": "Minute must be >= 0."})

    return _ensure_aware(match_part.start_time) + timedelta(minutes=minute)


class _MatchBoundWriteSerializer(serializers.Serializer):
    """Base serializer that expects match/match_data in context."""

    def _get_match(self) -> Match:
        match = self.context.get("match")
        if not isinstance(match, Match):
            raise serializers.ValidationError("Missing match context.")
        return match

    def _get_match_data(self) -> MatchData:
        match_data = self.context.get("match_data")
        if not isinstance(match_data, MatchData):
            raise serializers.ValidationError("Missing match_data context.")
        return match_data


class ShotWriteSerializer(_MatchBoundWriteSerializer):
    """Write serializer for creating/updating shots."""

    player_id = serializers.UUIDField()
    team_id = serializers.UUIDField()
    shot_type_id = serializers.UUIDField()
    match_part_id = serializers.UUIDField()
    for_team = serializers.BooleanField(required=False, default=True)
    scored = serializers.BooleanField(required=False, default=True)
    time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Validate incoming shot payload and resolve related objects.

        Raises:
            ValidationError: If the payload is invalid.

        """
        match = self._get_match()
        match_data = self._get_match_data()

        match_part = MatchPart.objects.filter(
            id_uuid=attrs["match_part_id"],
            match_data=match_data,
        ).first()
        if not match_part:
            raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})

        team_id = str(attrs["team_id"])  # UUIDField -> uuid.UUID
        if team_id not in {str(match.home_team.id_uuid), str(match.away_team.id_uuid)}:
            raise serializers.ValidationError({
                "team_id": "Team is not part of this match."
            })

        player = Player.objects.filter(id_uuid=attrs["player_id"]).first()
        if not player:
            raise serializers.ValidationError({"player_id": UNKNOWN_PLAYER})

        shot_type = GoalType.objects.filter(id_uuid=attrs["shot_type_id"]).first()
        if not shot_type:
            raise serializers.ValidationError({"shot_type_id": "Unknown goal type."})

        event_time = _resolve_event_time(
            match_part=match_part,
            time=_optional_str(attrs.get("time")),
            minute=_optional_int(attrs.get("minute")),
        )

        attrs["_match_part"] = match_part
        attrs["_player"] = player
        attrs["_shot_type"] = shot_type
        attrs["_event_time"] = event_time
        return attrs

    def create(self, validated_data: dict[str, object]) -> Shot:
        """Create a new shot instance from validated data.

        Raises:
            ValidationError: If the referenced team is unknown.

        """
        match_data = self._get_match_data()
        match_part = validated_data["_match_part"]
        player = validated_data["_player"]
        shot_type = validated_data["_shot_type"]
        event_time = validated_data["_event_time"]

        team_id = validated_data["team_id"]
        team = Team.objects.filter(id_uuid=team_id).first()
        if not team:
            raise serializers.ValidationError({"team_id": UNKNOWN_TEAM})

        return Shot.objects.create(
            match_data=match_data,
            match_part=match_part,
            player=player,
            team=team,
            shot_type=shot_type,
            for_team=bool(validated_data.get("for_team", True)),
            scored=bool(validated_data.get("scored", True)),
            time=event_time,
        )

    def update(self, instance: Shot, validated_data: dict[str, object]) -> Shot:
        """Update an existing shot instance."""
        match_data = self._get_match_data()

        _apply_shot_part_and_time(instance, validated_data, match_data)
        _apply_shot_player(instance, validated_data)
        _apply_shot_goal_type(instance, validated_data)
        _apply_shot_team(instance, validated_data)
        _apply_shot_flags(instance, validated_data)

        instance.save()
        return instance


def _apply_shot_part_and_time(
    shot: Shot,
    data: dict[str, object],
    match_data: MatchData,
) -> None:
    if {"match_part_id", "time", "minute"} & data.keys():
        match_part_id = data.get("match_part_id") or getattr(
            shot, "match_part_id", None
        )
        match_part = MatchPart.objects.filter(
            id_uuid=match_part_id,
            match_data=match_data,
        ).first()
        if not match_part:
            raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})
        shot.match_part = match_part
        shot.time = _resolve_event_time(
            match_part=match_part,
            time=_optional_str(data.get("time")),
            minute=_optional_int(data.get("minute")),
        )


def _apply_shot_player(shot: Shot, data: dict[str, object]) -> None:
    if "player_id" not in data:
        return
    player = Player.objects.filter(id_uuid=data["player_id"]).first()
    if not player:
        raise serializers.ValidationError({"player_id": UNKNOWN_PLAYER})
    shot.player = player


def _apply_shot_goal_type(shot: Shot, data: dict[str, object]) -> None:
    if "shot_type_id" not in data:
        return
    shot_type = GoalType.objects.filter(id_uuid=data["shot_type_id"]).first()
    if not shot_type:
        raise serializers.ValidationError({"shot_type_id": "Unknown goal type."})
    shot.shot_type = shot_type


def _apply_shot_team(shot: Shot, data: dict[str, object]) -> None:
    if "team_id" not in data:
        return
    team = Team.objects.filter(id_uuid=data["team_id"]).first()
    if not team:
        raise serializers.ValidationError({"team_id": UNKNOWN_TEAM})
    shot.team = team


def _apply_shot_flags(shot: Shot, data: dict[str, object]) -> None:
    if "for_team" in data:
        shot.for_team = bool(data["for_team"])
    if "scored" in data:
        shot.scored = bool(data["scored"])


class PlayerChangeWriteSerializer(_MatchBoundWriteSerializer):
    """Write serializer for creating/updating player changes."""

    player_in_id = serializers.UUIDField()
    player_out_id = serializers.UUIDField()
    player_group_id = serializers.UUIDField()
    match_part_id = serializers.UUIDField()
    time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Validate incoming player change payload and resolve related objects.

        Raises:
            ValidationError: If the payload is invalid.

        """
        match_data = self._get_match_data()

        match_part = MatchPart.objects.filter(
            id_uuid=attrs["match_part_id"],
            match_data=match_data,
        ).first()
        if not match_part:
            raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})

        player_groups = getattr(match_data, "player_groups", None)
        if player_groups is None:
            raise serializers.ValidationError({
                "player_group_id": "Player group relation not available."
            })
        player_group = (
            cast(Any, player_groups).filter(id_uuid=attrs["player_group_id"]).first()
        )
        if not player_group:
            raise serializers.ValidationError({
                "player_group_id": "Invalid player group."
            })

        player_in = Player.objects.filter(id_uuid=attrs["player_in_id"]).first()
        player_out = Player.objects.filter(id_uuid=attrs["player_out_id"]).first()
        if not player_in:
            raise serializers.ValidationError({"player_in_id": UNKNOWN_PLAYER})
        if not player_out:
            raise serializers.ValidationError({"player_out_id": UNKNOWN_PLAYER})

        event_time = _resolve_event_time(
            match_part=match_part,
            time=_optional_str(attrs.get("time")),
            minute=_optional_int(attrs.get("minute")),
        )

        attrs["_match_part"] = match_part
        attrs["_player_group"] = player_group
        attrs["_player_in"] = player_in
        attrs["_player_out"] = player_out
        attrs["_event_time"] = event_time
        return attrs

    def create(self, validated_data: dict[str, object]) -> PlayerChange:
        """Create a new player change instance from validated data."""
        match_data = self._get_match_data()
        return PlayerChange.objects.create(
            match_data=match_data,
            match_part=validated_data["_match_part"],
            player_group=validated_data["_player_group"],
            player_in=validated_data["_player_in"],
            player_out=validated_data["_player_out"],
            time=validated_data["_event_time"],
        )

    def update(
        self, instance: PlayerChange, validated_data: dict[str, object]
    ) -> PlayerChange:
        """Update an existing player change instance.

        Raises:
            ValidationError: If any referenced objects are invalid.

        """
        match_data = self._get_match_data()

        if (
            "match_part_id" in validated_data
            or "time" in validated_data
            or "minute" in validated_data
        ):
            match_part = MatchPart.objects.filter(
                id_uuid=validated_data.get(
                    "match_part_id",
                    getattr(instance, "match_part_id", None),
                ),
                match_data=match_data,
            ).first()
            if not match_part:
                raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})
            instance.match_part = match_part
            instance.time = _resolve_event_time(
                match_part=match_part,
                time=_optional_str(validated_data.get("time")),
                minute=_optional_int(validated_data.get("minute")),
            )

        if "player_group_id" in validated_data:
            player_groups = getattr(match_data, "player_groups", None)
            if player_groups is None:
                raise serializers.ValidationError({
                    "player_group_id": "Player group relation not available."
                })
            player_group = (
                cast(Any, player_groups)
                .filter(id_uuid=validated_data["player_group_id"])
                .first()
            )
            if not player_group:
                raise serializers.ValidationError({
                    "player_group_id": "Invalid player group."
                })
            instance.player_group = player_group

        if "player_in_id" in validated_data:
            player_in = Player.objects.filter(
                id_uuid=validated_data["player_in_id"]
            ).first()
            if not player_in:
                raise serializers.ValidationError({"player_in_id": UNKNOWN_PLAYER})
            instance.player_in = player_in

        if "player_out_id" in validated_data:
            player_out = Player.objects.filter(
                id_uuid=validated_data["player_out_id"]
            ).first()
            if not player_out:
                raise serializers.ValidationError({"player_out_id": UNKNOWN_PLAYER})
            instance.player_out = player_out

        instance.save()
        return instance


class PauseWriteSerializer(_MatchBoundWriteSerializer):
    """Write serializer for creating/updating pauses."""

    match_part_id = serializers.UUIDField()
    start_time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)
    length_seconds = serializers.IntegerField(required=False, min_value=0)
    active = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Validate incoming pause payload and resolve derived timestamps.

        Raises:
            ValidationError: If the payload is invalid.

        """
        match_data = self._get_match_data()
        match_part = MatchPart.objects.filter(
            id_uuid=attrs["match_part_id"],
            match_data=match_data,
        ).first()
        if not match_part:
            raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})

        start = _resolve_event_time(
            match_part=match_part,
            time=_optional_str(attrs.get("start_time")),
            minute=_optional_int(attrs.get("minute")),
        )
        length_seconds = _coerce_int(attrs.get("length_seconds"))
        end = start + timedelta(seconds=length_seconds) if length_seconds else None

        attrs["_match_part"] = match_part
        attrs["_start_time"] = start
        attrs["_end_time"] = end
        return attrs

    def create(self, validated_data: dict[str, object]) -> Pause:
        """Create a new pause instance from validated data."""
        match_data = self._get_match_data()
        return Pause.objects.create(
            match_data=match_data,
            match_part=validated_data["_match_part"],
            start_time=validated_data["_start_time"],
            end_time=validated_data.get("_end_time"),
            active=bool(validated_data.get("active")),
        )

    def update(self, instance: Pause, validated_data: dict[str, object]) -> Pause:
        """Update an existing pause instance.

        Raises:
            ValidationError: If the payload is invalid.

        """
        match_data = self._get_match_data()

        if "match_part_id" in validated_data:
            match_part = MatchPart.objects.filter(
                id_uuid=validated_data["match_part_id"],
                match_data=match_data,
            ).first()
            if not match_part:
                raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})
            instance.match_part = match_part

        if (
            "start_time" in validated_data
            or "minute" in validated_data
            or "length_seconds" in validated_data
        ):
            match_part_for_time = instance.match_part
            if match_part_for_time is None:
                raise serializers.ValidationError({
                    "match_part_id": "Pause has no match part."
                })
            start = _resolve_event_time(
                match_part=match_part_for_time,
                time=_optional_str(validated_data.get("start_time")),
                minute=_optional_int(validated_data.get("minute")),
            )
            length_seconds = _coerce_int(validated_data.get("length_seconds"))
            instance.start_time = start
            cast(Any, instance).end_time = (
                start + timedelta(seconds=length_seconds) if length_seconds else None
            )

        if "active" in validated_data:
            instance.active = bool(validated_data["active"])

        instance.save()
        return instance


class TimeoutWriteSerializer(_MatchBoundWriteSerializer):
    """Write serializer for creating/updating timeouts."""

    team_id = serializers.UUIDField()
    match_part_id = serializers.UUIDField()
    start_time = serializers.CharField(required=False, allow_blank=True)
    minute = serializers.IntegerField(required=False)
    length_seconds = serializers.IntegerField(required=False, min_value=0)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Validate incoming timeout payload and resolve related objects.

        Raises:
            ValidationError: If the payload is invalid.

        """
        match = self._get_match()
        match_data = self._get_match_data()

        match_part = MatchPart.objects.filter(
            id_uuid=attrs["match_part_id"],
            match_data=match_data,
        ).first()
        if not match_part:
            raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})

        team_id = str(attrs["team_id"])
        if team_id not in {str(match.home_team.id_uuid), str(match.away_team.id_uuid)}:
            raise serializers.ValidationError({
                "team_id": "Team is not part of this match."
            })

        start = _resolve_event_time(
            match_part=match_part,
            time=_optional_str(attrs.get("start_time")),
            minute=_optional_int(attrs.get("minute")),
        )
        length_seconds = _coerce_int(attrs.get("length_seconds"))
        end = start + timedelta(seconds=length_seconds) if length_seconds else None

        attrs["_match_part"] = match_part
        attrs["_start_time"] = start
        attrs["_end_time"] = end
        return attrs

    def create(self, validated_data: dict[str, object]) -> Timeout:
        """Create a new timeout instance from validated data.

        Raises:
            ValidationError: If the referenced team is unknown.

        """
        match_data = self._get_match_data()

        team_id = validated_data["team_id"]
        team = Team.objects.filter(id_uuid=team_id).first()
        if not team:
            raise serializers.ValidationError({"team_id": UNKNOWN_TEAM})

        pause = Pause.objects.create(
            match_data=match_data,
            match_part=validated_data["_match_part"],
            start_time=validated_data["_start_time"],
            end_time=validated_data.get("_end_time"),
            active=False,
        )

        return Timeout.objects.create(
            match_data=match_data,
            match_part=validated_data["_match_part"],
            team=team,
            pause=pause,
        )

    def update(self, instance: Timeout, validated_data: dict[str, object]) -> Timeout:
        """Update an existing timeout instance.

        Raises:
            ValidationError: If the payload is invalid.

        """
        match_data = self._get_match_data()

        if "team_id" in validated_data:
            team_id = validated_data["team_id"]
            team = Team.objects.filter(id_uuid=team_id).first()
            if not team:
                raise serializers.ValidationError({"team_id": UNKNOWN_TEAM})
            instance.team = team

        if instance.pause is None:
            raise serializers.ValidationError({"pause": "Timeout has no pause."})

        pause = instance.pause
        if "match_part_id" in validated_data:
            match_part = MatchPart.objects.filter(
                id_uuid=validated_data["match_part_id"],
                match_data=match_data,
            ).first()
            if not match_part:
                raise serializers.ValidationError({"match_part_id": INVALID_MATCH_PART})
            instance.match_part = match_part
            pause.match_part = match_part

        if (
            "start_time" in validated_data
            or "minute" in validated_data
            or "length_seconds" in validated_data
        ):
            match_part_for_time = pause.match_part
            if match_part_for_time is None:
                raise serializers.ValidationError({
                    "match_part_id": "Timeout pause has no match part."
                })
            start = _resolve_event_time(
                match_part=match_part_for_time,
                time=_optional_str(validated_data.get("start_time")),
                minute=_optional_int(validated_data.get("minute")),
            )
            length_seconds = _coerce_int(validated_data.get("length_seconds"))
            pause.start_time = start
            cast(Any, pause).end_time = (
                start + timedelta(seconds=length_seconds) if length_seconds else None
            )

        pause.save()
        instance.save()
        return instance
