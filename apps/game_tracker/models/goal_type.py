"""Model for goal type."""

from bg_uuidv7 import uuidv7
from django.db import models


class GoalType(models.Model):
    """Model for goal type."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True, default=uuidv7, editable=False,
    )
    name: models.CharField = models.CharField(max_length=255, unique=True)

    def __str__(self) -> str:
        """Return the string representation of the goal type."""
        return str(self.name)
