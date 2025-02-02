from django.contrib.auth.models import User
from django.db import models
from django_cryptography.fields import encrypt


class SpotifyToken(models.Model):
    """Model for SpotifyToken."""

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    access_token = encrypt(models.CharField(max_length=500))
    refresh_token = encrypt(models.CharField(max_length=500))
    expires_at = models.DateTimeField()
    spotify_user_id = models.CharField(max_length=100, unique=True)

    def is_token_expired(self):
        """Check if the Spotify token is expired."""
        from django.utils.timezone import now

        return self.expires_at and now() > self.expires_at
