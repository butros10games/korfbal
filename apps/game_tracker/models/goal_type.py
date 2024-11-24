"""Model for goal type."""

from uuidv7 import uuid7

from django.db import models


class GoalType(models.Model):
    """Model for goal type."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        """Return the string representation of the goal type."""
        return str(self.name)
