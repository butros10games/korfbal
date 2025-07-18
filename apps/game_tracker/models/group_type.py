"""Model for GroupType."""

from bg_uuidv7 import uuidv7
from django.db import models


class GroupType(models.Model):
    """Model for GroupType."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True, default=uuidv7, editable=False,
    )
    name: models.CharField = models.CharField(max_length=255, unique=True)
    order: models.IntegerField = models.IntegerField(default=0)

    def __str__(self) -> str:
        """Return the string representation of the group type."""
        return str(self.name)
