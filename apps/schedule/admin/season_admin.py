"""Admin class for the Season model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.schedule.models import Season


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    SeasonAdminBase = ModelAdminBase[Season]
else:
    SeasonAdminBase = KorfbalModelAdmin


@admin.register(Season)
class SeasonAdmin(SeasonAdminBase):
    """Admin settings for the Season model."""

    ordering = ("-start_date",)

    list_display = ("name", "start_date", "end_date")
    search_fields = ("id_uuid", "name")
    show_full_result_count = False

    class Meta:
        """Meta class for the Season model."""

        model = Season
