"""Admin class for the PageConnectRegistration model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.hub.models import PageConnectRegistration
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PageConnectRegistrationAdminBase = ModelAdminBase[PageConnectRegistration]
else:
    PageConnectRegistrationAdminBase = KorfbalModelAdmin


@admin.register(PageConnectRegistration)
class PageConnectRegistrationAdmin(PageConnectRegistrationAdminBase):
    """PageConnectRegistration admin configuration."""

    list_select_related = ("player__user",)
    search_fields = ("id_uuid", "player__name", "player__user__username", "page")
    list_filter = ("registration_date",)
    ordering = ("-registration_date",)

    list_display = ("player", "page", "registration_date")
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PageConnectRegistration
