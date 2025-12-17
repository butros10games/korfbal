"""Module contains the CachedSong model for shared song downloads."""

from __future__ import annotations

from bg_uuidv7 import uuidv7
from django.db import models


class CachedSongStatus(models.TextChoices):
    """Lifecycle states for cached downloads."""

    QUEUED = "queued", "Queued"
    DOWNLOADING = "downloading", "Downloading"
    UPLOADING = "uploading", "Uploading"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class CachedSong(models.Model):
    """A cached audio download for a Spotify track.

    Multiple players can reference the same CachedSong to avoid downloading
    the same track multiple times.
    """

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    spotify_url: models.URLField = models.URLField(max_length=500, unique=True)

    title: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    artists: models.CharField[str, str] = models.CharField(max_length=255, blank=True)
    duration_seconds: models.IntegerField[int, int | None] = models.IntegerField(
        null=True, blank=True
    )

    status: models.CharField[str, str] = models.CharField(
        max_length=20,
        choices=CachedSongStatus.choices,
        default=CachedSongStatus.QUEUED,
    )
    error_message: models.TextField[str, str] = models.TextField(blank=True)

    audio_file: models.FileField = models.FileField(
        upload_to="cached_songs/",
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
        return f"{self.id_uuid}: {label}"
