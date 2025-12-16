"""Module contains the PlayerSong model for the player app."""

from __future__ import annotations

from bg_uuidv7 import uuidv7
from django.db import models

from .player import Player


class PlayerSongStatus(models.TextChoices):
    """Lifecycle states for downloaded songs."""

    QUEUED = "queued", "Queued"
    DOWNLOADING = "downloading", "Downloading"
    UPLOADING = "uploading", "Uploading"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class PlayerSong(models.Model):
    """A song the player added via a Spotify link.

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

    spotify_url: models.URLField = models.URLField(max_length=500)

    title: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    artists: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    duration_seconds: models.IntegerField[int, int | None] = models.IntegerField(
        null=True, blank=True
    )

    start_time_seconds: models.IntegerField[int, int] = models.IntegerField(default=0)

    status: models.CharField[str, str] = models.CharField(
        max_length=20,
        choices=PlayerSongStatus.choices,
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

        ordering = ("-created_at",)

    def __str__(self) -> str:
        """Return a readable representation for admin/debugging."""
        label = self.title or self.spotify_url
        return f"{self.player.id_uuid}: {label}"
