"""File contains signals for the Player model."""

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.player.models import Player


@receiver(post_save, sender=User)
def create_player_for_new_user(
    sender: User,
    instance: User,
    created: bool,
    **kwargs: dict[str, str] | None,
) -> None:
    """Create a Player instance when a new user is created.

    Args:
        sender (Model): The model class.
        instance (User): The user instance.
        created (bool): Whether the user is created.
        **kwargs: Additional keyword arguments.

    """
    if created:
        # If the user is just created, create a Player instance
        Player.objects.create(user=instance)
