"""Team app configuration."""

from importlib import import_module

from django.apps import AppConfig


class TeamConfig(AppConfig):
    """Team app configuration."""

    name = "apps.team"

    def ready(self) -> None:
        """Register history tracking for native roster mutations."""
        import_module("apps.team.signals")
