from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.schedule.models import Match
from apps.game_tracker.models import MatchData


@receiver(post_save, sender=Match)
def create_matchdata_for_new_match(sender, instance, created, **kwargs):
    if created:
        # If the Match is just created, create a MatchData instance
        MatchData.objects.create(match_link=instance)
