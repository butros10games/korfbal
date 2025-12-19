"""Module contains the Club model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bg_uuidv7 import uuidv7
from django.db import models
from django.templatetags.static import static
from django.urls import reverse


if TYPE_CHECKING:
    from apps.team.models.team import Team


class Club(models.Model):
    """Model for a club."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True, default=uuidv7, editable=False
    )
    name: models.CharField[str, str] = models.CharField(max_length=255, unique=True)
    admin: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        "player.Player",  # type: ignore[misc]
        through="ClubAdmin",
        related_name="clubs",
        blank=True,
    )
    logo: models.ImageField = models.ImageField(
        upload_to="club_pictures/",
        blank=True,
        null=True,
    )

    if TYPE_CHECKING:
        teams: models.QuerySet[Team]

    def __str__(self) -> str:
        """Return the name of the club.

        Returns:
            str: The name of the club.

        """
        return str(self.name)

    def get_absolute_url(self) -> str:
        """Get the absolute URL for the club detail page.

        Returns:
            str: The absolute URL of the club detail page.

        """
        return reverse("club_detail", kwargs={"club_id": self.id_uuid})

    def get_club_logo(self) -> str:
        """Get the URL of the club logo.

        Returns:
            str: The URL of the club logo or a default image if not set.

        """
        if self.logo:
            return self.logo.url  # type: ignore[no-any-return]

        # Optional: if the KWT club doesn't have an uploaded logo in the DB,
        # serve a known static brand asset.
        if (self.name or "").strip().lower() == "kwt":
            return static("images/logo/KWT_logo.png")

        return static("images/clubs/blank-club-picture.png")
