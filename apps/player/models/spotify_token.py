"""Model for SpotifyToken."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.contrib.auth.models import User
from django.db import models
from django.utils.timezone import now


class SpotifyToken(models.Model):
    """Model for SpotifyToken."""

    user: models.OneToOneField[Any, Any] = models.OneToOneField(
        User, on_delete=models.CASCADE
    )
    access_token: models.CharField[str, str] = models.CharField(max_length=500)
    refresh_token: models.CharField[str, str] = models.CharField(max_length=500)
    expires_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    spotify_user_id: models.CharField[str, str] = models.CharField(
        max_length=100, unique=True
    )

    def __str__(self) -> str:
        """Return the string representation of the SpotifyToken.

        Returns:
            str: A string representation of the SpotifyToken instance.

        """
        return f"SpotifyToken(user={self.user.username}, spotify_user_id={self.spotify_user_id})"  # noqa: E501

    def is_token_expired(self) -> bool:
        """Check if the Spotify token is expired.

        Returns:
            bool: True if the token is expired, False otherwise.

        """
        return now() > self.expires_at
