"""App configuration for the game_tracker app."""

from importlib import import_module

from django.apps import AppConfig


class GameTrackerConfig(AppConfig):
    """App configuration for the game_tracker app."""

    name = "apps.game_tracker"

    def ready(self) -> None:
        """Import signals when the app is ready."""
        for module in (
            "impact_recompute_signals",
            "match_data_deletion_signals",
            "match_data_signals",
            "match_signals",
            "realtime_update_signals",
        ):
            import_module(f"apps.game_tracker.signals.{module}")
