"""Video analysis application registration."""

from django.apps import AppConfig


class VideoAnalysisConfig(AppConfig):
    """Own review data and background analysis work."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.video_analysis"
