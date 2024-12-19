"""Model for a season."""

from django.db import models
from uuidv7 import uuid7


class Season(models.Model):
    """Model for a season."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()

    def __str__(self):
        """Return the string representation of the season."""
        return str(self.name)
