"""Serializers for player API endpoints."""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.models import User
from rest_framework import serializers

from apps.player.models.player import Player


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model."""

    class Meta:
        """Meta class for UserSerializer."""

        model = User
        fields: ClassVar[list[str]] = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
        ]


class PlayerSerializer(serializers.ModelSerializer):
    """Serializer for Player model."""

    user = UserSerializer(read_only=True)
    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        """Meta class for PlayerSerializer."""

        model = Player
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "user",
            "profile_picture",
            "profile_picture_url",
            "team_follow",
            "club_follow",
            "goal_song_uri",
            "song_start_time",
        ]
        read_only_fields: ClassVar[list[str]] = ["id_uuid", "user"]

    def get_profile_picture_url(self, obj: Player) -> str:  # noqa: PLR6301
        """Return the profile picture URL.

        Args:
            obj (Player): The player instance.

        Returns:
            str: The URL of the profile picture.

        """
        return obj.get_profile_picture()
