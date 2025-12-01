"""Serializers for team API endpoints."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from apps.club.api.serializers import ClubSerializer
from apps.team.models.team import Team


class TeamSerializer(serializers.ModelSerializer):
    """Serializer for Team model."""

    club = ClubSerializer(read_only=True)
    club_id = serializers.UUIDField(write_only=True)

    class Meta:
        """Meta class for TeamSerializer."""

        model = Team
        fields: ClassVar[list[str]] = ["id_uuid", "name", "club", "club_id"]
        read_only_fields: ClassVar[list[str]] = ["id_uuid"]
