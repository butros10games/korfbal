"""Django application configuration for tournament mode."""

from importlib import import_module

from django.apps import AppConfig


class TournamentConfig(AppConfig):
    """Configure tournament mode."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tournament"

    def ready(self) -> None:
        """Register lifecycle signals after Django loads the app registry."""
        import_module("apps.tournament.signals")
