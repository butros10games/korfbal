"""Module contains the Player model for the player app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .constants import club_model_string, team_model_string


if TYPE_CHECKING:
    from datetime import date

    from django.db.models import QuerySet

    from apps.club.models.club import Club


class Player(models.Model):
    """Model for Player."""

    class Visibility(models.TextChoices):
        """Visibility options for profile data."""

        PUBLIC = "public", "Public"
        CLUB = "club", "Club"
        PRIVATE = "private", "Private"

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    user: models.OneToOneField[Any, Any] = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="player",
    )

    profile_picture: models.ImageField = models.ImageField(
        upload_to="profile_pictures/",
        blank=True,
        null=True,
    )

    profile_picture_visibility: models.CharField[str, str] = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )

    stats_visibility: models.CharField[str, str] = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )

    teams_visibility: models.CharField[str, str] = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )

    team_follow: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        team_model_string,
        blank=True,
    )
    club_follow: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        club_model_string,
        blank=True,
    )

    # Club membership is distinct from "follows": it represents the real-world
    # affiliation of a player to a club and supports history (start/end dates).
    member_clubs: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        club_model_string,
        through="PlayerClubMembership",
        related_name="members",
        blank=True,
    )

    goal_song_uri: models.CharField[str, str] = models.CharField(
        max_length=255, blank=True
    )
    song_start_time: models.IntegerField[int | None, int | None] = models.IntegerField(
        blank=True, null=True
    )

    # Preferred goal-song configuration: a list of PlayerSong UUIDs in the order
    # they should be cycled through.
    goal_song_song_ids: models.JSONField[list[str]] = models.JSONField(
        default=list,
        blank=True,
    )

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
        # The legacy Django-rendered `profile_detail` route was removed when the
        # project migrated to a React SPA. Profile links should point into the SPA.
        return f"{settings.WEB_APP_ORIGIN}/players/{self.id_uuid}"

    def active_member_clubs(self, *, on: date | None = None) -> QuerySet[Club]:
        """Return clubs this player is a member of at the given date."""
        if on is None:
            on = timezone.localdate()

        return (
            self.member_clubs
            .filter(
                player_membership_links__player=self,
                player_membership_links__start_date__lte=on,
            )
            .filter(
                Q(player_membership_links__end_date__isnull=True)
                | Q(player_membership_links__end_date__gte=on)
            )
            .distinct()
        )

    def get_profile_picture(self) -> str:
        """Get the URL of the player's profile picture.

        Returns:
            str: The URL of the profile picture or a default image URL.

        """
        if self.profile_picture:
            return self.profile_picture.url

        return self.get_placeholder_profile_picture_url()

    def get_placeholder_profile_picture_url(self) -> str:
        """Return the default placeholder profile picture URL."""
        static_url = cast(str, settings.STATIC_URL).removeprefix("/")
        return f"https://{static_url}images/player/blank-profile-picture.png"
