"""Model file for Team."""

from bg_uuidv7 import uuidv7
from django.db import models
from django.urls import reverse


class Team(models.Model):
    """Model for Team."""

    id_uuid: models.UUIDField = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    name: models.CharField = models.CharField(max_length=255)
    club: models.ForeignKey = models.ForeignKey(
        "club.Club",
        on_delete=models.CASCADE,
        related_name="teams",
    )

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
        return reverse("team_detail", kwargs={"team_id": self.id_uuid})
