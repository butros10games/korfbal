"""Module contains the PlayerSong model for the player app."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import TYPE_CHECKING, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models
from django.db.models import Q
from django.db.models.fields.files import FieldFile

from apps.player.media_paths import player_song_path

from .cached_song import CachedSong
from .player import Player


if TYPE_CHECKING:
    from apps.team.models.team_data import TeamData


class PlayerSongStatus(models.TextChoices):
    """Lifecycle states for downloaded songs."""

    QUEUED = "queued", "Queued"
    DOWNLOADING = "downloading", "Downloading"
    UPLOADING = "uploading", "Uploading"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class PlayerSong(models.Model):
    """A song imported from Spotify, YouTube or an uploaded audio file.

    The actual audio is imported asynchronously and stored in the
    configured Django storage backend (S3/MinIO).
    """

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    player: models.ForeignKey[Player | None, Player | None] = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="songs",
        null=True,
        blank=True,
    )
    player_id: str | None
    team_data: models.ForeignKey[TeamData | None, TeamData | None] = models.ForeignKey(
        "team.TeamData",
        on_delete=models.CASCADE,
        related_name="songs",
        null=True,
        blank=True,
    )
    team_data_id: int | None

    # Shared cached download for this track.
    cached_song: models.ForeignKey[CachedSong, CachedSong] = models.ForeignKey(
        CachedSong,
        on_delete=models.PROTECT,
        related_name="player_entries",
        blank=True,
        null=True,
    )
    cached_song_id: str | None

    # Historical column name; canonical Spotify/YouTube URL, empty for uploads.
    spotify_url: models.URLField = models.URLField(
        max_length=500,
        blank=True,
        default="",
    )

    title: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    artists: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    duration_seconds: models.IntegerField[int | None, int | None] = models.IntegerField(
        null=True, blank=True
    )

    start_time_seconds: models.IntegerField[int, int] = models.IntegerField(default=0)
    clip_name: models.CharField[str, str] = models.CharField(max_length=80, blank=True)
    clip_duration_seconds: models.PositiveSmallIntegerField[int, int] = (
        models.PositiveSmallIntegerField(default=8)
    )

    # Playback speed used by previews and tracker audio.
    playback_speed: models.FloatField[float, float] = models.FloatField(default=1.0)

    status: models.CharField[str, str] = models.CharField(
        max_length=20,
        choices=PlayerSongStatus,
        default=PlayerSongStatus.QUEUED,
    )
    error_message: models.TextField[str, str] = models.TextField(blank=True)

    audio_file: models.FileField = models.FileField(
        upload_to=player_song_path,
        blank=True,
        null=True,
    )

    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        ordering: ClassVar[list[str]] = ["-created_at"]
        constraints = (
            models.CheckConstraint(
                condition=(
                    Q(player__isnull=False, team_data__isnull=True)
                    | Q(player__isnull=True, team_data__isnull=False)
                ),
                name="song_has_one_owner",
            ),
            models.CheckConstraint(
                condition=Q(
                    clip_duration_seconds__gte=1, clip_duration_seconds__lte=15
                ),
                name="song_clip_duration_bounds",
            ),
        )

    def __str__(self) -> str:
        """Return a readable representation for admin/debugging."""
        label = self.title or self.spotify_url or "(uploaded song)"
        return f"{self.player_id or self.team_data_id}: {label}"

    @property
    def source_id(self) -> str:
        """Group clips by their immutable audio without exposing a storage key."""
        if self.cached_song_id is not None:
            return str(self.cached_song_id)
        key = self.audio_file.name or str(self.pk)
        return hashlib.sha256(key.encode()).hexdigest()

    @property
    def effective_audio_file(self) -> FieldFile:
        """Return shared audio when present, otherwise player-owned audio."""
        return (
            self.cached_song.audio_file
            if self.cached_song is not None
            else self.audio_file
        )

    @property
    def effective_status(self) -> str:
        """Return the lifecycle status of the effective audio source."""
        return self.cached_song.status if self.cached_song is not None else self.status

    @property
    def effective_error_message(self) -> str:
        """Return the error reported by the effective audio source."""
        return (
            self.cached_song.error_message
            if self.cached_song is not None
            else self.error_message
        )

    @property
    def effective_title(self) -> str:
        """Return shared metadata when present, otherwise player metadata."""
        return self.cached_song.title if self.cached_song is not None else self.title

    @property
    def effective_artists(self) -> str:
        """Return artists from the effective audio source."""
        return (
            self.cached_song.artists if self.cached_song is not None else self.artists
        )

    @property
    def effective_duration_seconds(self) -> int | None:
        """Return duration from the effective audio source."""
        return (
            self.cached_song.duration_seconds
            if self.cached_song is not None
            else self.duration_seconds
        )

    @property
    def effective_updated_at(self) -> datetime:
        """Return the revision timestamp of the effective audio source."""
        return (
            self.cached_song.updated_at
            if self.cached_song is not None
            else self.updated_at
        )
