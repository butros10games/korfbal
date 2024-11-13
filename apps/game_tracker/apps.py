from django.apps import AppConfig


class GameTrackerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.game_tracker"

    def ready(self):
        import apps.game_tracker.signals
