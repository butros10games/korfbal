"""Model file for Team."""

from uuidv7 import uuid7

from django.db import models
from django.urls import reverse


class Team(models.Model):
    """Model for Team."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255)
    club = models.ForeignKey(
        "club.Club", on_delete=models.CASCADE, related_name="teams"
    )

    def __str__(self):
        """Return the string representation of the team."""
        return str(self.club.name) + " " + str(self.name)

    def get_absolute_url(self):
        """Return the absolute URL of the team."""
        return reverse("team_detail", kwargs={"team_id": self.id_uuid})
