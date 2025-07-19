"""Player app configuration."""

from django.apps import AppConfig


class PlayerConfig(AppConfig):
    """Player app configuration."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.player"

    def ready(self) -> None:  # noqa: PLR6301
        """Import signals."""
        import apps.player.signals  # noqa
