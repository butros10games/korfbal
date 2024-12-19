"""This module contains the Player model for the player app."""

from django.contrib.auth.models import User
from django.db import models
from django.urls import reverse
from uuidv7 import uuid7

from .constants import club_model_string, team_model_string


class Player(models.Model):
    """Model for Player."""

    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="players")

    profile_picture = models.ImageField(
        upload_to="profile_pictures/",
        default="/static/images/player/blank-profile-picture.png",
        blank=True,
    )

    team_follow = models.ManyToManyField(team_model_string, blank=True)
    club_follow = models.ManyToManyField(club_model_string, blank=True)

    def __str__(self):
        """Return the string representation of the player."""
        return str(self.user.username)

    def get_absolute_url(self):
        """Return the absolute URL of the player."""
        return reverse("profile_detail", kwargs={"player_id": self.id_uuid})

    def get_profile_picture(self):
        """Return the profile picture of the player."""
        if "static" in self.profile_picture.url:
            return self.profile_picture.url
        else:
            return "/media" + self.profile_picture.url
