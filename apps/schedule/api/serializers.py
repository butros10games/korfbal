"""Serializers for schedule API endpoints."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from apps.schedule.models import Match
from apps.team.api.serializers import TeamSerializer


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
