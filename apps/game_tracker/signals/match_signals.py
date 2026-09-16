"""Signals for the Match model."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.game_tracker.composition import record_match_change
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match


@receiver(post_save, sender=Match)
def create_match_data_for_new_match(
    sender: type[Match],
    instance: Match,
    created: bool,
    update_fields: frozenset[str] | None = None,
    **kwargs: object,
) -> None:
    """Create a MatchData instance for a new Match instance.

    Args:
        sender: The sender of the signal.
        instance: The instance of the Match model.
        created: A boolean indicating if the instance was created.
        update_fields: Fields explicitly saved, when supplied.
        **kwargs: Additional keyword arguments.

    """
    if created:
        # If the Match is just created, ensure a MatchData instance exists.
        MatchData.objects.get_or_create(match_link=instance)

    elif not update_fields or {
        "home_team",
        "away_team",
        "home_team_id",
        "away_team_id",
    }.intersection(update_fields):
        match_data = MatchData.objects.filter(match_link=instance).first()
        if match_data is not None:
            record_match_change(match_data)
