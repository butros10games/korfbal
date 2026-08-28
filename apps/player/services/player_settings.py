"""Application services for mutable player settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from apps.player.models.player import Player


PRIVACY_FIELDS: Final[tuple[str, ...]] = (
    "profile_picture_visibility",
    "stats_visibility",
    "teams_visibility",
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
