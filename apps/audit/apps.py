"""App configuration for the audit app."""

from django.apps import AppConfig


class AuditConfig(AppConfig):
    """App configuration for the audit app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
