"""Serializers for team API endpoints."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from apps.club.api.serializers import ClubSerializer
from apps.club.models.club import Club
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models.team import Team


class TeamSerializer(serializers.ModelSerializer):
    """Serializer for Team model."""

    club = ClubSerializer(read_only=True)
    club_id = serializers.PrimaryKeyRelatedField(
        source="club",
        queryset=Club.objects.all(),
        write_only=True,
    )

    class Meta:
        """Meta class for TeamSerializer."""

        model = Team
        fields: ClassVar[list[str]] = ["id_uuid", "name", "club", "club_id"]
        read_only_fields: ClassVar[list[str]] = ["id_uuid"]


class TeamRosterMutationSerializer(serializers.Serializer):
    """Validate an incremental season membership change."""

    player = serializers.PrimaryKeyRelatedField(queryset=Player.objects.all())
    operation = serializers.ChoiceField(choices=["add", "remove"])


class TeamPoolMatchesQuerySerializer(serializers.Serializer):
    """Require an explicit season and match status for pool browsing."""

    season = serializers.PrimaryKeyRelatedField(queryset=Season.objects.all())
    status = serializers.ChoiceField(choices=["upcoming", "finished"])


class TeamPoolMatchesPageSerializer(serializers.Serializer):
    """Document the paginated public match-summary response."""

    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = serializers.ListField(child=serializers.DictField())
