"""External competition catalogue, independent of locally managed teams."""

from django.apps import AppConfig


class CompetitionConfig(AppConfig):
    """Register competition data storage."""

    name = "apps.competition"
    default_auto_field = "django.db.models.BigAutoField"
