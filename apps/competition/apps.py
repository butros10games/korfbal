"""KNKV source identities and synchronization for shared application records."""

from django.apps import AppConfig


class CompetitionConfig(AppConfig):
    """Register competition data storage."""

    name = "apps.competition"
    verbose_name = "KNKV source data"
    default_auto_field = "django.db.models.BigAutoField"
