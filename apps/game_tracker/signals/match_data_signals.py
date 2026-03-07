"""Signals for the MatchData model."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.game_tracker.models import GroupType, MatchData
from apps.game_tracker.services.player_groups import (
    ensure_player_groups_for_group_type,
    ensure_player_groups_for_match_data,
)


@receiver(post_save, sender=MatchData)
def create_player_groups_for_new_match_data(
    sender: type[MatchData],
    instance: MatchData,
    created: bool,
    **kwargs: str,
) -> None:
    """Create player groups for a new match data instance.

    Args:
        sender: The sender of the signal.
        instance: The instance of the MatchData model.
        created: A boolean indicating if the instance was created.
        **kwargs: Additional keyword arguments.

    """
    if not created:
        return

    ensure_player_groups_for_match_data(instance)


@receiver(post_save, sender=GroupType)
def create_player_groups_for_new_group_type(
    sender: type[GroupType],
    instance: GroupType,
    created: bool,
    **kwargs: str,
) -> None:
    """Backfill player groups when a new GroupType is introduced."""
    del sender, kwargs
    if not created:
        return

    ensure_player_groups_for_group_type(instance)
