"""Model for GroupType."""

from __future__ import annotations

from typing import ClassVar

from bg_uuidv7 import uuidv7
from django.db import models


class GroupType(models.Model):
    """Model for GroupType."""

    objects: ClassVar[models.Manager[GroupType]]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    name: models.CharField[str, str] = models.CharField(max_length=255, unique=True)
    order: models.IntegerField[int, int] = models.IntegerField(default=0)

    def __str__(self) -> str:
        """Return the string representation of the group type.

        Returns:
            str: A string representation of the group type.

        """
        return str(self.name)
