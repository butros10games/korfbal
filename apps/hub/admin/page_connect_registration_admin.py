"""Admin class for the PageConnectRegistration model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.hub.models import PageConnectRegistration


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PageConnectRegistrationAdminBase = ModelAdminBase[PageConnectRegistration]
else:
    PageConnectRegistrationAdminBase = admin.ModelAdmin


class PageConnectRegistrationAdmin(PageConnectRegistrationAdminBase):
    """PageConnectRegistration admin configuration."""

    list_display = ["id_uuid", "player", "page", "registration_date"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PageConnectRegistration


admin.site.register(PageConnectRegistration, PageConnectRegistrationAdmin)
