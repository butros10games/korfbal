"""Model for a season."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

from bg_uuidv7 import uuidv7
from django.db import models


class Season(models.Model):
    """Model for a season."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    name: models.CharField[str, str] = models.CharField(max_length=255, unique=True)
    start_date: models.DateField[date, date] = models.DateField()
    end_date: models.DateField[date, date] = models.DateField()

    class Meta:
        """Require a valid inclusive season interval."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="season_valid_date_interval",
            )
        ]

    def __str__(self) -> str:
        """Get the string representation of the season.

        Returns:
            str: The name of the season.

        """
        return str(self.name)
