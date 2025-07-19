"""Admin class for the PageConnectRegistration model."""

from django.contrib import admin

from apps.hub.models import PageConnectRegistration


class PageConnectRegistrationAdmin(admin.ModelAdmin):
    """PageConnectRegistration admin configuration."""

    list_display = ["id_uuid", "player", "page", "registration_date"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PageConnectRegistration


admin.site.register(PageConnectRegistration, PageConnectRegistrationAdmin)
