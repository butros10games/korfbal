"""Admin settings for the Timeout model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import Timeout


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    TimeoutAdminBase = ModelAdminBase[Timeout]
else:
    TimeoutAdminBase = admin.ModelAdmin


class TimeoutAdmin(TimeoutAdminBase):
    """Admin for the Timeout model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "match_data",
        "team",
        "match_part",
        "pause",
    ]
    list_filter: ClassVar[list[str]] = ["team", "match_part"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "match_data__id_uuid",
        "team__name",
    ]
    autocomplete_fields: ClassVar[list[str]] = [
        "match_data",
        "match_part",
        "team",
        "pause",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = Timeout


admin.site.register(Timeout, TimeoutAdmin)
