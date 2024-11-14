from apps.player.models import Player
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User)
def create_player_for_new_user(sender, instance, created, **kwargs):
    if created:
        # If the user is just created, create a Player instance
        Player.objects.create(user=instance)
