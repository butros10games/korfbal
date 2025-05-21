"""Model for a page connect registration."""

from django.db import models
from uuidv7 import uuid7

from .constants import player_model_string


class PageConnectRegistration(models.Model):
    """Model for a page connect registration for a player."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True, default=uuid7, editable=False
    )
    player: models.ForeignKey = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="page_connect_registrations",
    )
    page: models.CharField = models.CharField(max_length=255)
    registration_date: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return the string representation of the page connect registration."""
        return f"{self.player} - {self.page}"
