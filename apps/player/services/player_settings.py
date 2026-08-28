"""Application services for mutable player settings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final, cast

from django.db import transaction
from django.db.models import Model

from apps.player.models.player import Player


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


def delete_player_profile(player: Player) -> None:
    """Delete a Player profile without deleting its user account."""
    player.delete()
