"""Module contains the Player model for the player app."""

from __future__ import annotations

from datetime import timedelta
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


class VisiblePlayerManager(models.Manager):
    """Keep withdrawn or stale source-only identities out of ordinary app queries."""

    def get_queryset(self) -> models.QuerySet:
        """Retain the existing account privacy rules for registered players."""
        return (
            super()
            .get_queryset()
            .filter(
                Q(user_id__isnull=False)
                | Q(knkv_person_id__isnull=True)
                | Q(
                    knkv_privacy__in=("OPEN", "NORMAL", "LIMITED"),
                    knkv_observed_at__gte=timezone.now() - timedelta(days=8),
                )
            )
        )


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
        blank=True,
        null=True,
    )
    user_id: int | None
    name = models.CharField(max_length=255, blank=True)
    knkv_person_id = models.CharField(max_length=80, blank=True, null=True, unique=True)
    knkv_photo = models.CharField(max_length=255, blank=True)
    knkv_privacy = models.CharField(max_length=16, blank=True)
    knkv_observed_at = models.DateTimeField(null=True, blank=True)

    date_of_birth: models.DateField[date, date | None] = models.DateField(
        blank=True,
        null=True,
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

    all_objects = models.Manager()
    objects = VisiblePlayerManager()

    class Meta:
        """Keep normal reads filtered and relation integrity unfiltered."""

        default_manager_name = "objects"
        base_manager_name = "all_objects"

    def __str__(self) -> str:
        """Get the string representation of the player.

        Returns:
            str: The username of the player.

        """
        return self.display_name

    def get_absolute_url(self) -> str:
        """Get the absolute URL for the player's profile detail view.

        Returns:
            str: The URL to the player's profile detail view.

        """
        # The legacy Django-rendered `profile_detail` route was removed when the
        # project migrated to a React SPA. Profile links should point into the SPA.
        return f"{settings.WEB_APP_ORIGIN}/players/{self.id_uuid}"

    @property
    def display_name(self) -> str:
        """Display a player independently of whether they have a login account."""
        if self.user_id:
            return self.name or str(self.user.username)
        if self.knkv_person_id and (
            self.knkv_privacy not in {"OPEN", "NORMAL", "LIMITED"}
            or self.knkv_observed_at is None
            or self.knkv_observed_at < timezone.now() - timedelta(days=8)
        ):
            return "Afgeschermd"
        return self.name or "Afgeschermd"

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
