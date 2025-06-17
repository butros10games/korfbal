"""Model for a season."""

from bg_uuidv7 import uuidv7
from django.db import models


class Season(models.Model):
    """Model for a season."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True, default=uuidv7, editable=False
    )
    name: models.CharField = models.CharField(max_length=255, unique=True)
    start_date: models.DateField = models.DateField()
    end_date: models.DateField = models.DateField()

    def __str__(self) -> str:
        """Return the string representation of the season."""
        return str(self.name)
