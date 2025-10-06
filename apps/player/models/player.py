"""Module contains the Player model for the player app."""

from typing import cast

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.urls import reverse

from .constants import club_model_string, team_model_string


class Player(models.Model):
    """Model for Player."""

    id_uuid = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="player",
    )

    profile_picture = models.ImageField(
        upload_to="profile_pictures/",
        blank=True,
        null=True,
    )

    team_follow = models.ManyToManyField(  # type: ignore[var-annotated]
        team_model_string,
        blank=True,
    )
    club_follow = models.ManyToManyField(  # type: ignore[var-annotated]
        club_model_string,
        blank=True,
    )

    goal_song_uri = models.CharField(max_length=255, blank=True)
    song_start_time = models.IntegerField(blank=True, null=True)

    def __str__(self) -> str:
        """Get the string representation of the player.

        Returns:
            str: The username of the player.

        """
        return str(self.user.username)

    def get_absolute_url(self) -> str:
        """Get the absolute URL for the player's profile detail view.

        Returns:
            str: The URL to the player's profile detail view.

        """
        return reverse("profile_detail", kwargs={"player_id": self.id_uuid})

    def get_profile_picture(self) -> str:
        """Get the URL of the player's profile picture.

        Returns:
            str: The URL of the profile picture or a default image URL.

        """
        if self.profile_picture:
            return self.profile_picture.url  # type: ignore[no-any-return]
        static_url = cast(str, settings.STATIC_URL).removeprefix("/")
        return f"https://{static_url}images/player/blank-profile-picture.png"
