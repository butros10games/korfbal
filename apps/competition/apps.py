"""KNKV source identities and synchronization for shared application records."""

from importlib import import_module

from django.apps import AppConfig


class CompetitionConfig(AppConfig):
    """Register competition data storage."""

    name = "apps.competition"
    verbose_name = "KNKV source data"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Wire the outbound job runtime after models are registered."""
        import_module("apps.competition.adapters.outbound.job_runtime")
