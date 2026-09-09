"""Application services for mutable player settings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final, cast

from django.db import transaction
from django.db.models import Model, Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.player.models.player import Player
from apps.team.models import TeamData


PRIVACY_FIELDS: Final[tuple[str, ...]] = (
    "profile_picture_visibility",
    "stats_visibility",
    "teams_visibility",
)
PROFILE_RELATION_FIELDS: Final[tuple[str, ...]] = (
    "team_follow",
    "club_follow",
    "member_clubs",
)


def player_privacy_settings(player: Player) -> dict[str, str]:
    """Return normalized privacy settings for API consumers."""
    settings: dict[str, str] = {}
    for field in PRIVACY_FIELDS:
        value = str(getattr(player, field))
        settings[field] = (
            Player.Visibility.CLUB if value == Player.Visibility.PRIVATE else value
        )
    return settings


def update_player_privacy_settings(
    *,
    player: Player,
    changes: Mapping[str, object],
) -> None:
    """Persist validated privacy-setting changes for one player."""
    update_fields: list[str] = []
    for field in PRIVACY_FIELDS:
        if field not in changes:
            continue
        setattr(player, field, str(changes[field]))
        update_fields.append(field)

    if update_fields:
        player.save(update_fields=update_fields)


@transaction.atomic
def update_player_account(
    *,
    player: Player,
    username: str,
    email: str,
) -> None:
    """Persist account fields owned by the player's Django user."""
    user = player.user
    user.username = username
    user.email = email
    user.save(update_fields=["username", "email"])


@transaction.atomic
def change_player_password(*, player: Player, new_password: str) -> None:
    """Persist a validated password for the player's Django user."""
    user = player.user
    user.set_password(new_password)
    user.save(update_fields=["password"])


@transaction.atomic
def update_player_profile(
    *,
    player: Player,
    changes: Mapping[str, object],
) -> None:
    """Persist validated scalar and relationship profile changes."""
    scalar_fields: list[str] = []
    relationships: dict[str, Iterable[Model]] = {}

    for field, value in changes.items():
        if field in PROFILE_RELATION_FIELDS:
            relationships[field] = cast(Iterable[Model], value)
            continue
        setattr(player, field, value)
        scalar_fields.append(field)

    if scalar_fields:
        player.save(update_fields=scalar_fields)
    for field, values in relationships.items():
        manager = getattr(player, field)
        manager.set(values)


@transaction.atomic
def delete_player_profile(player: Player) -> None:
    """Remove a profile while retaining a private identity required by history."""
    # Roster and team-song commands lock the seasonal team before the player.
    # Follow that order when clearing current memberships during archival.
    roster_ids = TeamData.objects.filter(
        Q(players=player) | Q(coach=player) | Q(staff=player)
    ).values("pk")
    list(
        TeamData.objects
        .select_for_update(no_key=True)
        .filter(pk__in=roster_ids)
        .order_by("pk")
    )
    player = Player.all_objects.select_for_update().get(pk=player.pk)
    try:
        with transaction.atomic():
            player.delete()
        return
    except ProtectedError:
        pass
    picture = player.profile_picture
    player.user = None
    player.name = ""
    player.date_of_birth = None
    player.profile_picture = None
    player.knkv_photo = ""
    player.knkv_privacy = ""
    player.knkv_observed_at = None
    player.goal_song_uri = ""
    player.song_start_time = None
    player.goal_song_song_ids = []
    player.archived_at = timezone.now()
    for field in PRIVACY_FIELDS:
        setattr(player, field, Player.Visibility.PRIVATE)
    player.save()
    player.team_follow.clear()
    player.club_follow.clear()
    player.clubs.clear()
    player.songs.all().delete()
    for relation in ("team_data_as_player", "team_data_as_coach", "team_data_as_staff"):
        getattr(player, relation).clear()
    if picture:
        transaction.on_commit(lambda: picture.delete(save=False))
