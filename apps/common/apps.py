"""Common app configuration."""

from django.apps import AppConfig


class TeamConfig(AppConfig):
    """App configuration for the common app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
