"""Signals for the Match model."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.game_tracker.models import MatchData
from apps.schedule.models import Match


@receiver(post_save, sender=Match)
def create_match_data_for_new_match(
    sender: type[Match], instance: Match, created: bool, **kwargs: str,
) -> None:
    """Create a MatchData instance for a new Match instance.

    Args:
        sender: The sender of the signal.
        instance: The instance of the Match model.
        created: A boolean indicating if the instance was created.
        **kwargs: Additional keyword arguments.

    """
    if created:
        # If the Match is just created, create a MatchData instance
        MatchData.objects.create(match_link=instance)
