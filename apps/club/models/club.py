"""Module contains the Club model."""

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models
from django.urls import reverse


class Club(models.Model):
    """Model for a club."""

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    admin = models.ManyToManyField(
        "player.Player",
        through="ClubAdmin",
        related_name="clubs",
        blank=True,
    )
    logo = models.ImageField(
        upload_to="club_pictures/",
        blank=True,
        null=True,
    )

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
            return self.logo.url
        static_url = settings.STATIC_URL.removeprefix("/")
        return f"https://{static_url}images/clubs/blank-club-picture.png"
