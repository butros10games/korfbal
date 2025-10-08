"""Model for a page connect registration."""

from datetime import datetime
from typing import Any

from bg_uuidv7 import uuidv7
from django.db import models

from .constants import player_model_string


class PageConnectRegistration(models.Model):
    """Model for a page connect registration for a player."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        player_model_string,
        on_delete=models.CASCADE,
        related_name="page_connect_registrations",
    )
    page: models.CharField[str, str] = models.CharField(max_length=255)
    registration_date: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self) -> str:
        """Return the string representation of the page connect registration.

        Returns:
            str: String representation of the page connect registration.

        """
        return f"{self.player} - {self.page}"
