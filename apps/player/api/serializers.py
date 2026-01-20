"""Serializers for player API endpoints."""

from __future__ import annotations

from typing import Any, ClassVar, cast
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import serializers

from apps.club.api.serializers import ClubSerializer
from apps.player.models.cached_song import CachedSong
from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.privacy import can_view_by_visibility


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
    viewer_is_superuser = serializers.SerializerMethodField()
    profile_picture_url = serializers.SerializerMethodField()
    goal_song_songs = serializers.SerializerMethodField()
    active_member_clubs = serializers.SerializerMethodField()
    can_view_profile_picture = serializers.SerializerMethodField()
    can_view_stats = serializers.SerializerMethodField()
    can_view_teams = serializers.SerializerMethodField()
    is_private_account = serializers.SerializerMethodField()

    _viewer_player: Player | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Initialise the serializer and resolve the viewer player (if any)."""
        # DRF's `Serializer.__init__` accepts a wide set of keyword arguments;
        # we cast to keep strict typing (and satisfy ruff's ANN401 rules).
        super().__init__(*args, **cast(dict[str, Any], kwargs))

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            # Cache once per serializer instance to avoid repeated queries.
            # If we're serializing the *current* player, avoid a redundant DB
            # lookup by reusing the instance.
            instance = getattr(self, "instance", None)
            if isinstance(instance, Player) and getattr(
                instance, "user_id", None
            ) == getattr(
                user,
                "id",
                None,
            ):
                self._viewer_player = instance
            else:
                self._viewer_player = Player.objects.filter(user=user).first()

    class Meta:
        """Meta class for PlayerSerializer."""

        model = Player
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "user",
            "viewer_is_superuser",
            "profile_picture",
            "profile_picture_url",
            "profile_picture_visibility",
            "stats_visibility",
            "teams_visibility",
            "can_view_profile_picture",
            "can_view_stats",
            "can_view_teams",
            "is_private_account",
            "team_follow",
            "club_follow",
            "member_clubs",
            "active_member_clubs",
            "goal_song_uri",
            "song_start_time",
            "goal_song_song_ids",
            "goal_song_songs",
        ]
        read_only_fields: ClassVar[list[str]] = [
            "id_uuid",
            "user",
            "viewer_is_superuser",
            "can_view_profile_picture",
            "can_view_stats",
            "can_view_teams",
            "is_private_account",
        ]

    def get_viewer_is_superuser(self, obj: Player) -> bool:
        """Return whether the requesting user is a Django superuser.

        Notes:
            This is intentionally a *viewer* flag (not a property of `obj.user`),
            so we don't leak privilege information about the profiled user.

        """
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(getattr(user, "is_superuser", False))

    def get_profile_picture_url(self, obj: Player) -> str:
        """Return the profile picture URL.

        Args:
            obj (Player): The player instance.

        Returns:
            str: The URL of the profile picture.

        """
        if self.get_can_view_profile_picture(obj):
            return obj.get_profile_picture()
        return obj.get_placeholder_profile_picture_url()

    def get_can_view_profile_picture(self, obj: Player) -> bool:
        """Return True if the requesting viewer may see the profile picture."""
        return can_view_by_visibility(
            visibility=obj.profile_picture_visibility,
            viewer=self._viewer_player,
            target=obj,
        )

    def get_can_view_stats(self, obj: Player) -> bool:
        """Return True if the requesting viewer may see the player's stats."""
        return can_view_by_visibility(
            visibility=obj.stats_visibility,
            viewer=self._viewer_player,
            target=obj,
        )

    def get_can_view_teams(self, obj: Player) -> bool:
        """Return True if the requesting viewer may see the player's teams."""
        return can_view_by_visibility(
            visibility=obj.teams_visibility,
            viewer=self._viewer_player,
            target=obj,
        )

    def get_is_private_account(self, obj: Player) -> bool:
        """Return True when both stats and profile picture are blocked."""
        return (not self.get_can_view_profile_picture(obj)) and (
            not self.get_can_view_stats(obj)
        )

    # NOTE: `rest_framework` serializers inherit from `Field`, and type stubs + ty
    # can disagree about the effective override target. This implementation matches
    # DRF's runtime contract (returns a dict for API responses) and we scope-ignore
    # only the override check.
    def to_representation(
        self,
        instance: object,
    ) -> dict[str, Any]:
        """Serialise a Player while minimising PII exposure for non-self views."""
        player = instance
        if not isinstance(player, Player):
            # Fall back to DRF's default behavior for unexpected instance types.
            return super().to_representation(instance)

        data = super().to_representation(player)

        # Do not expose the deprecated 'private' option to clients.
        if data.get("profile_picture_visibility") == Player.Visibility.PRIVATE:
            data["profile_picture_visibility"] = Player.Visibility.CLUB
        if data.get("stats_visibility") == Player.Visibility.PRIVATE:
            data["stats_visibility"] = Player.Visibility.CLUB
        if data.get("teams_visibility") == Player.Visibility.PRIVATE:
            data["teams_visibility"] = Player.Visibility.CLUB

        is_self = (
            self._viewer_player is not None
            and self._viewer_player.id_uuid == player.id_uuid
        )

        # Minimise exposure of personal data for other players.
        if not is_self:
            user = data.get("user")
            if isinstance(user, dict):
                user.pop("email", None)
                user.pop("first_name", None)
                user.pop("last_name", None)

        if not self.get_can_view_profile_picture(player):
            # Prevent leaking the raw file path/URL via the ImageField.
            data["profile_picture"] = None

        if (not is_self) and (not self.get_can_view_teams(player)):
            # Do not leak follow preferences when teams are hidden.
            data["team_follow"] = []

        if (not is_self) and self.get_is_private_account(player):
            # When the viewer can't see anything meaningful, avoid exposing
            # follow relations (club/team preferences).
            data["team_follow"] = []
            data["club_follow"] = []
            data["member_clubs"] = []
            data["active_member_clubs"] = []
        return data

    def get_active_member_clubs(self, obj: Player) -> list[dict[str, object]]:
        """Return active club memberships as embedded Club objects."""
        clubs = obj.active_member_clubs()
        data = ClubSerializer(clubs, many=True, context=self.context).data
        return cast(list[dict[str, object]], data)

    def get_goal_song_songs(self, obj: Player) -> list[dict[str, object]]:
        """Return ordered goal-song info for cycling.

        This is derived from `Player.goal_song_song_ids` and returns only songs
        that exist and have an audio file.
        """
        ids = [song_id for song_id in (obj.goal_song_song_ids or []) if song_id]
        if not ids:
            return []

        songs = list(
            PlayerSong.objects.select_related("cached_song").filter(
                player=obj,
                id_uuid__in=ids,
            )
        )
        by_id = {str(song.id_uuid): song for song in songs}

        ordered: list[dict[str, object]] = []
        for song_id in ids:
            song = by_id.get(song_id)
            audio_file = None
            if song is not None:
                audio_file = (
                    song.cached_song.audio_file
                    if song.cached_song is not None
                    else song.audio_file
                )
            if song is None or not audio_file:
                continue

            # Provide a short clip URL (8s) so clients don't download full tracks.
            request = self.context.get("request")
            clip_path = reverse(
                "player-song-clip",
                kwargs={"song_id": str(song.id_uuid)},
            )
            clip_query = urlencode({
                "start": int(song.start_time_seconds or 0),
                "duration": 8,
            })
            clip_url = f"{clip_path}?{clip_query}"
            if request is not None and hasattr(request, "build_absolute_uri"):
                clip_url = request.build_absolute_uri(clip_url)

            ordered.append({
                "id_uuid": str(song.id_uuid),
                "audio_url": clip_url,
                # Keep start time so clients can still seek when the clip endpoint
                # falls back to the full audio file.
                "start_time_seconds": song.start_time_seconds,
            })

        return ordered


class PlayerSongSerializer(serializers.ModelSerializer):
    """Serializer for PlayerSong model."""

    title = serializers.SerializerMethodField()
    artists = serializers.SerializerMethodField()
    duration_seconds = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    error_message = serializers.SerializerMethodField()
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
            "playback_speed",
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

    @staticmethod
    def _cached(obj: PlayerSong) -> CachedSong | None:
        """Return the linked CachedSong when present."""
        return getattr(obj, "cached_song", None)

    def get_title(self, obj: PlayerSong) -> str:
        """Return the song title (from cache when linked)."""
        cached = self._cached(obj)
        return cached.title if cached is not None else obj.title

    def get_artists(self, obj: PlayerSong) -> str:
        """Return the song artists (from cache when linked)."""
        cached = self._cached(obj)
        return cached.artists if cached is not None else obj.artists

    def get_duration_seconds(self, obj: PlayerSong) -> int | None:
        """Return the song duration in seconds (from cache when linked)."""
        cached = self._cached(obj)
        return cached.duration_seconds if cached is not None else obj.duration_seconds

    def get_status(self, obj: PlayerSong) -> str:
        """Return the effective lifecycle status (from cache when linked)."""
        cached = self._cached(obj)
        return cached.status if cached is not None else obj.status

    def get_error_message(self, obj: PlayerSong) -> str:
        """Return the effective error message (from cache when linked)."""
        cached = self._cached(obj)
        return cached.error_message if cached is not None else obj.error_message

    def get_audio_url(self, obj: PlayerSong) -> str | None:
        """Return the resolved audio URL when available."""
        cached = self._cached(obj)
        audio_file = cached.audio_file if cached is not None else obj.audio_file
        if not audio_file:
            return None
        return audio_file.url


class PlayerSongCreateSerializer(serializers.Serializer):
    """Input serializer for creating a PlayerSong."""

    spotify_url = serializers.URLField(
        max_length=500,
        required=False,
        allow_blank=True,
    )
    audio_file = serializers.FileField(required=False, allow_empty_file=False)

    @staticmethod
    def _validate_audio_file(uploaded: object) -> None:
        """Validate an uploaded MP3 file.

        Raises:
            ValidationError: When the uploaded file is not an MP3 or exceeds limits.

        """
        name = getattr(uploaded, "name", "") or ""
        content_type = getattr(uploaded, "content_type", "") or ""
        size = int(getattr(uploaded, "size", 0) or 0)

        if not name.lower().endswith(".mp3"):
            raise serializers.ValidationError({
                "audio_file": "Only MP3 uploads are supported."
            })

        if content_type and content_type not in {"audio/mpeg", "audio/mp3"}:
            # Some browsers omit/lie; only enforce when provided.
            raise serializers.ValidationError({
                "audio_file": "Invalid content type (expected MP3)."
            })

        # Guardrail to avoid accidental huge uploads.
        max_bytes = 25 * 1024 * 1024
        if size and size > max_bytes:
            raise serializers.ValidationError({
                "audio_file": "File is too large (max 25MB)."
            })

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Require either spotify_url or audio_file.

        Also validates that uploaded files look like MP3.

        Raises:
            ValidationError: When neither spotify_url nor audio_file is provided,
                or when the uploaded file is invalid.

        """
        spotify_url = attrs.get("spotify_url")
        audio_file = attrs.get("audio_file")

        spotify_url_str = str(spotify_url).strip() if spotify_url is not None else ""

        if not spotify_url_str and audio_file is None:
            raise serializers.ValidationError("Provide spotify_url or audio_file.")

        if audio_file is not None:
            self._validate_audio_file(audio_file)

        # Normalise whitespace.
        attrs["spotify_url"] = spotify_url_str

        return attrs


class PlayerSongUpdateSerializer(serializers.Serializer):
    """Input serializer for updating PlayerSong settings."""

    start_time_seconds = serializers.IntegerField(min_value=0, required=False)
    playback_speed = serializers.FloatField(
        min_value=0.5,
        max_value=2.0,
        required=False,
    )

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Require at least one updatable field.

        Raises:
            ValidationError: When neither `start_time_seconds` nor `playback_speed`
                is provided.

        """
        if "start_time_seconds" not in attrs and "playback_speed" not in attrs:
            raise serializers.ValidationError(
                "Provide start_time_seconds and/or playback_speed."
            )
        return attrs


class PlayerPrivacySettingsSerializer(serializers.Serializer):
    """Input serializer for updating privacy visibility settings."""

    profile_picture_visibility = serializers.ChoiceField(
        choices=Player.Visibility.choices,
        required=False,
    )
    stats_visibility = serializers.ChoiceField(
        choices=Player.Visibility.choices,
        required=False,
    )
    teams_visibility = serializers.ChoiceField(
        choices=Player.Visibility.choices,
        required=False,
    )

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Validate and normalise privacy settings input.

        Notes:
            The deprecated visibility option 'private' is coerced to 'club'.

        Raises:
            ValidationError: When no settings are provided.

        """
        if not attrs:
            raise serializers.ValidationError("Provide at least one privacy setting.")

        # Backwards compatibility: if an older client sends 'private', treat it
        # as 'club' (the stricter, still-useful option).
        if attrs.get("profile_picture_visibility") == Player.Visibility.PRIVATE:
            attrs["profile_picture_visibility"] = Player.Visibility.CLUB
        if attrs.get("stats_visibility") == Player.Visibility.PRIVATE:
            attrs["stats_visibility"] = Player.Visibility.CLUB
        if attrs.get("teams_visibility") == Player.Visibility.PRIVATE:
            attrs["teams_visibility"] = Player.Visibility.CLUB

        return attrs


class PlayerPushSubscriptionSerializer(serializers.ModelSerializer):
    """Output serializer for stored push subscriptions."""

    class Meta:
        """Serializer metadata."""

        model = PlayerPushSubscription
        fields: ClassVar[list[str]] = [
            "id_uuid",
            "endpoint",
            "platform",
            "is_active",
            "user_agent",
            "created_at",
            "updated_at",
        ]


class PlayerPushSubscriptionCreateSerializer(serializers.Serializer):
    """Input serializer for registering a push subscription."""

    subscription = serializers.JSONField()
    user_agent = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=512,
    )
    platform = serializers.ChoiceField(
        choices=("web", "expo"),
        required=False,
        default="web",
    )

    def validate_subscription(self, value: object) -> dict[str, Any]:
        """Validate that a PushSubscription JSON object looks sane.

        Supports both web push payloads (endpoint + keys) and Expo push tokens.

        Raises:
            ValidationError: If the object is missing required fields.

        """
        if not isinstance(value, dict):
            raise serializers.ValidationError("subscription must be an object")

        # JSONField yields an untyped dict; cast for strict type checking.
        payload = cast(dict[str, Any], value)

        endpoint = payload.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise serializers.ValidationError("subscription.endpoint is required")

        # Expo push tokens only provide an endpoint.
        if endpoint.startswith("ExponentPushToken["):
            return payload

        keys = payload.get("keys")
        if not isinstance(keys, dict):
            raise serializers.ValidationError("subscription.keys is required")

        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not isinstance(p256dh, str) or not p256dh.strip():
            raise serializers.ValidationError("subscription.keys.p256dh is required")
        if not isinstance(auth, str) or not auth.strip():
            raise serializers.ValidationError("subscription.keys.auth is required")

        return payload


class PlayerPushSubscriptionDeactivateSerializer(serializers.Serializer):
    """Input serializer for deactivating a stored subscription."""

    endpoint = serializers.URLField(max_length=1024, required=False)
    id_uuid = serializers.UUIDField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Require either endpoint or id_uuid.

        Raises:
            ValidationError: If neither endpoint nor id_uuid is provided.

        """
        if not attrs.get("endpoint") and not attrs.get("id_uuid"):
            raise serializers.ValidationError("Provide endpoint or id_uuid.")
        return attrs
