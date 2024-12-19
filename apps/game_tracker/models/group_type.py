"""Model for GroupType."""

from django.db import models
from uuidv7 import uuid7


class GroupType(models.Model):
    """Model for GroupType."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    order = models.IntegerField(default=0)

    def __str__(self):
        """Return the string representation of the group type."""
        return str(self.name)
