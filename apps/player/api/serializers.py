"""Serializers for player API endpoints."""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.models import User
from rest_framework import serializers

from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong


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

    def get_profile_picture_url(self, obj: Player) -> str:
        """Return the profile picture URL.

        Args:
            obj (Player): The player instance.

        Returns:
            str: The URL of the profile picture.

        """
        return obj.get_profile_picture()


class PlayerSongSerializer(serializers.ModelSerializer):
    """Serializer for PlayerSong model."""

    audio_url = serializers.SerializerMethodField()

    class Meta:
        """Meta class for PlayerSongSerializer."""

        model = PlayerSong
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "spotify_url",
            "title",
            "artists",
            "duration_seconds",
            "start_time_seconds",
            "status",
            "error_message",
            "audio_url",
            "created_at",
            "updated_at",
        ]
        read_only_fields: ClassVar[list[str]] = [
            "id_uuid",
            "title",
            "artists",
            "duration_seconds",
            "status",
            "error_message",
            "audio_url",
            "created_at",
            "updated_at",
        ]

    def get_audio_url(self, obj: PlayerSong) -> str | None:
        """Return the resolved audio URL when available."""
        if not obj.audio_file:
            return None
        return obj.audio_file.url  # type: ignore[no-any-return]


class PlayerSongCreateSerializer(serializers.Serializer):
    """Input serializer for creating a PlayerSong."""

    spotify_url = serializers.URLField(max_length=500)


class PlayerSongUpdateSerializer(serializers.Serializer):
    """Input serializer for updating PlayerSong settings."""

    start_time_seconds = serializers.IntegerField(min_value=0)
