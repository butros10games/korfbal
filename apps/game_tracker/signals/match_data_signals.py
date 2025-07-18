"""Signals for the MatchData model."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.game_tracker.models import GroupType, MatchData, PlayerGroup


@receiver(post_save, sender=MatchData)
def create_player_groups_for_new_match_data(
    sender: type[MatchData], instance: MatchData, created: bool, **kwargs: str,
) -> None:
    """Create player groups for a new match data instance.

    Args:
        sender: The sender of the signal.
        instance: The instance of the MatchData model.
        created: A boolean indicating if the instance was created.
        **kwargs: Additional keyword arguments.

    """
    if created:
        all_group_types = GroupType.objects.all()
        for group_type in all_group_types:
            PlayerGroup.objects.create(
                match_data=instance,
                starting_type=group_type,
                current_type=group_type,
                team=instance.match_link.home_team,
            )

        for group_type in all_group_types:
            PlayerGroup.objects.create(
                match_data=instance,
                starting_type=group_type,
                current_type=group_type,
                team=instance.match_link.away_team,
            )
