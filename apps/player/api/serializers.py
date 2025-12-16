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
    goal_song_songs = serializers.SerializerMethodField()

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
            "goal_song_song_ids",
            "goal_song_songs",
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

    def get_goal_song_songs(self, obj: Player) -> list[dict[str, object]]:
        """Return ordered goal-song info for cycling.

        This is derived from `Player.goal_song_song_ids` and returns only songs
        that exist and have an audio file.
        """
        ids = [song_id for song_id in (obj.goal_song_song_ids or []) if song_id]
        if not ids:
            return []

        songs = list(PlayerSong.objects.filter(player=obj, id_uuid__in=ids))
        by_id = {str(song.id_uuid): song for song in songs}

        ordered: list[dict[str, object]] = []
        for song_id in ids:
            song = by_id.get(song_id)
            if song is None or not song.audio_file:
                continue
            ordered.append({
                "id_uuid": str(song.id_uuid),
                "audio_url": song.audio_file.url,
                "start_time_seconds": song.start_time_seconds,
            })

        return ordered


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
