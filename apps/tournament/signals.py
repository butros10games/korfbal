"""Keep required one-to-one tournament defaults present."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.tournament.models import Tournament, TournamentDisplayConfig


@receiver(post_save, sender=Tournament)
def ensure_tournament_display_config(
    sender: type[Tournament],
    instance: Tournament,
    created: bool,
    **kwargs: object,
) -> None:
    """Create the presentation configuration for every tournament."""
    del sender, kwargs
    if created:
        TournamentDisplayConfig.objects.get_or_create(tournament=instance)
