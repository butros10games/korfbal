"""Admin settings for the GroupType model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import GroupType


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    GroupTypeAdminBase = ModelAdminBase[GroupType]
else:
    GroupTypeAdminBase = admin.ModelAdmin


class GroupTypeAdmin(GroupTypeAdminBase):
    """Admin for the GroupType model."""

    list_display = ["id_uuid", "name"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the GroupTypeAdmin."""

        model = GroupType


admin.site.register(GroupType, GroupTypeAdmin)
