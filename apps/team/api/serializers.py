"""Serializers for team API endpoints."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from apps.club.api.serializers import ClubCatalogSerializer, ClubSerializer
from apps.club.models.club import Club
from apps.player.api.serializers import PlayerSongSerializer
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models.team import Team


class TeamClipLibrarySerializer(PlayerSongSerializer):
    """A reusable team clip with provenance and an owner-scoped import receipt."""

    team_name = serializers.CharField(source="team_data.team.name", read_only=True)
    club_name = serializers.CharField(source="team_data.team.club.name", read_only=True)
    season_name = serializers.CharField(source="team_data.season.name", read_only=True)
    already_added = serializers.BooleanField(source="library_added", read_only=True)

    class Meta(PlayerSongSerializer.Meta):
        """Extend the existing clip contract without exposing personal owners."""

        fields: ClassVar[list[str]] = [
            *PlayerSongSerializer.Meta.fields,
            "team_name",
            "club_name",
            "season_name",
            "already_added",
        ]


class TeamClipLibraryQuerySerializer(serializers.Serializer):
    """Bound shared library search input."""

    search = serializers.CharField(
        required=False, allow_blank=True, max_length=100, default=""
    )


class TeamClipLibraryAddSerializer(serializers.Serializer):
    """Select one library source to copy into the authorized team."""

    source_id = serializers.UUIDField()


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


class TeamCatalogSerializer(TeamSerializer):
    """Include each team's club city in discovery results."""

    club = ClubCatalogSerializer(read_only=True)


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
