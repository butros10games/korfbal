"""Signals for the MatchData model."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.game_tracker.models import GroupType
from apps.game_tracker.services.player_groups import (
    ensure_player_groups_for_group_type,
)


@receiver(post_save, sender=GroupType)
def create_player_groups_for_new_group_type(
    sender: type[GroupType],
    instance: GroupType,
    created: bool,
    **kwargs: str,
) -> None:
    """Add a new GroupType to existing lineups.

    New matches no longer receive groups on creation; lineup and tracker entry
    points create them on first use (see ``ensure_player_groups_for_match_data``).
    """
    del sender, kwargs
    if not created:
        return

    ensure_player_groups_for_group_type(instance)
