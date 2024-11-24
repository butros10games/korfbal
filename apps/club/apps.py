"""App configuration for the club app."""

from django.apps import AppConfig


class ClubConfig(AppConfig):
    """App configuration for the club app."""

    default_auto_field = "django.db.models.BigAutoField"  # type: ignore
    name = "apps.club"
