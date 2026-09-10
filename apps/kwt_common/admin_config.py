"""Initialize the local admin site and authentication presentation."""

from importlib import import_module

from django.contrib.admin.apps import AdminConfig


class KorfbalAdminConfig(AdminConfig):
    """Select the local site and adapt authentication admins after discovery."""

    default_site = "apps.kwt_common.admin_site.KorfbalAdminSite"

    def ready(self) -> None:
        """Apply local overrides after shared authentication admin registration."""
        super().ready()
        import_module("apps.kwt_common.admin_integrations").configure_auth_admins()
