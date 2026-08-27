"""App configuration for the game_tracker app."""

from django.apps import AppConfig


class GameTrackerConfig(AppConfig):
    """App configuration for the game_tracker app."""

    name = "apps.game_tracker"

    def ready(self) -> None:
        """Import signals when the app is ready."""
        import apps.game_tracker.signals
