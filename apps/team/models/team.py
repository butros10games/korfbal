"""Model file for Team."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models


class Team(models.Model):
    """Model for Team."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    name: models.CharField[str, str] = models.CharField(max_length=255)
    club: models.ForeignKey[Any, Any] = models.ForeignKey(
        "club.Club",
        on_delete=models.CASCADE,
        related_name="teams",
    )

    class Meta:
        """Keep a global team identity unique within its club."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("club", "name"), name="unique_team_name_per_club"
            )
        ]

    club_id: UUID

    def __str__(self) -> str:
        """Get the string representation of the team.

        Returns:
            str: The name of the team with the club name.

        """
        return str(self.club.name) + " " + str(self.name)

    def get_absolute_url(self) -> str:
        """Get the absolute URL for the team detail view.

        Returns:
            str: The URL to the team detail view.

        """
        return f"{settings.WEB_APP_ORIGIN}/teams/{self.id_uuid}"
