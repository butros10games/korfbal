"""Module contains the PlayerSong model for the player app."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from bg_uuidv7 import uuidv7
from django.db import models
from django.db.models import Q
from django.db.models.fields.files import FieldFile

from .cached_song import CachedSong
from .player import Player


class PlayerSongStatus(models.TextChoices):
    """Lifecycle states for downloaded songs."""

    QUEUED = "queued", "Queued"
    DOWNLOADING = "downloading", "Downloading"
    UPLOADING = "uploading", "Uploading"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class PlayerSong(models.Model):
    """A song the player added via Spotify or an uploaded audio file.

    The actual audio is downloaded (spotDL) asynchronously and stored in the
    configured Django storage backend (S3/MinIO).
    """

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    player: models.ForeignKey[Player, Player] = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="songs",
    )
    player_id: str

    # Shared cached download for this track.
    cached_song: models.ForeignKey[CachedSong, CachedSong] = models.ForeignKey(
        CachedSong,
        on_delete=models.PROTECT,
        related_name="player_entries",
        blank=True,
        null=True,
    )
    cached_song_id: str | None

    # Spotify link when the song was added via Spotify.
    # Uploaded songs can leave this empty.
    spotify_url: models.URLField = models.URLField(
        max_length=500,
        blank=True,
        default="",
    )

    title: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    artists: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    duration_seconds: models.IntegerField[int, int | None] = models.IntegerField(
        null=True, blank=True
    )

    start_time_seconds: models.IntegerField[int, int] = models.IntegerField(default=0)

    # Playback speed used in the web UI preview (and any future local playback).
    playback_speed: models.FloatField[float, float] = models.FloatField(default=1.0)

    status: models.CharField[str, str] = models.CharField(
        max_length=20,
        choices=PlayerSongStatus,
        default=PlayerSongStatus.QUEUED,
    )
    error_message: models.TextField[str, str] = models.TextField(blank=True)

    audio_file: models.FileField = models.FileField(
        upload_to="player_songs/",
        blank=True,
        null=True,
    )

    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        ordering: ClassVar[list[str]] = ["-created_at"]
        constraints = (
            models.UniqueConstraint(
                fields=["player", "cached_song"],
                condition=Q(cached_song__isnull=False),
                name="unique_player_cached_song",
            ),
        )

    def __str__(self) -> str:
        """Return a readable representation for admin/debugging."""
        label = self.title or self.spotify_url or "(uploaded song)"
        return f"{self.player.id_uuid}: {label}"

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
