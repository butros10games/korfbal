from django.db import models
from django.urls import reverse
from uuidv7 import uuid7

from .constants import team_model_string, club_model_string

from django.contrib.auth.models import User


class Player(models.Model):
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
        return str(self.user.username)

    def get_absolute_url(self):
        return reverse("profile_detail", kwargs={"player_id": self.id_uuid})

    def get_profile_picture(self):
        if "static" in self.profile_picture.url:
            return self.profile_picture.url
        else:
            return "/media" + self.profile_picture.url
