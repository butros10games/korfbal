"""Admin configuration for season pools."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.schedule.models import SeasonPool


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    SeasonPoolAdminBase = ModelAdminBase[SeasonPool]
else:
    SeasonPoolAdminBase = admin.ModelAdmin


class SeasonPoolAdmin(SeasonPoolAdminBase):
    """Show pool membership alongside the existing schedule models."""

    list_display = ("id_uuid", "name", "season")
    list_filter = ("season",)
    search_fields = ("id_uuid", "name", "season__name")
    filter_horizontal = ("teams",)
    show_full_result_count = False


admin.site.register(SeasonPool, SeasonPoolAdmin)
