"""Model for SpotifyToken."""

from django.contrib.auth.models import User
from django.db import models


class SpotifyToken(models.Model):
    """Model for SpotifyToken."""

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    access_token = models.CharField(max_length=500)
    refresh_token = models.CharField(max_length=500)
    expires_at = models.DateTimeField()
    spotify_user_id = models.CharField(max_length=100, unique=True)

    def __str__(self) -> str:
        """Return the string representation of the SpotifyToken."""
        return f"SpotifyToken(user={self.user.username}, spotify_user_id={self.spotify_user_id})"  # noqa: E501

    def is_token_expired(self) -> bool:
        """Check if the Spotify token is expired."""
        from django.utils.timezone import now

        return self.expires_at and now() > self.expires_at
