"""Admin configuration for season pools."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.schedule.models import SeasonPool


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    SeasonPoolAdminBase = ModelAdminBase[SeasonPool]
else:
    SeasonPoolAdminBase = KorfbalModelAdmin


@admin.register(SeasonPool)
class SeasonPoolAdmin(SeasonPoolAdminBase):
    """Show pool membership alongside the existing schedule models."""

    list_select_related = ("season",)
    ordering = ("-season__start_date", "name")

    list_display = ("name", "season")
    list_filter = ("season",)
    search_fields = ("id_uuid", "name", "season__name")
    filter_horizontal = ()
    show_full_result_count = False
