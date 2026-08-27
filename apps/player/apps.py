"""Player app configuration."""

from django.apps import AppConfig


class PlayerConfig(AppConfig):
    """Player app configuration."""

    name = "apps.player"

    def ready(self) -> None:
        """Import signals."""
        import apps.player.signals
