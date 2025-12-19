"""Admin class for the Season model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.schedule.models import Season


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    SeasonAdminBase = ModelAdminBase[Season]
else:
    SeasonAdminBase = admin.ModelAdmin


class SeasonAdmin(SeasonAdminBase):
    """Admin settings for the Season model."""

    list_display: ClassVar[list[str]] = ["id_uuid", "name"]
    search_fields: ClassVar[list[str]] = ["id_uuid", "name"]
    show_full_result_count = False

    class Meta:
        """Meta class for the Season model."""

        model = Season


admin.site.register(Season, SeasonAdmin)
