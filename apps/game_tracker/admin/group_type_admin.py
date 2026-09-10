"""Admin settings for the GroupType model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import GroupType
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    GroupTypeAdminBase = ModelAdminBase[GroupType]
else:
    GroupTypeAdminBase = KorfbalModelAdmin


@admin.register(GroupType)
class GroupTypeAdmin(GroupTypeAdminBase):
    """Admin for the GroupType model."""

    search_fields = ("id_uuid", "name")
    ordering = ("order", "name")

    list_display = ("name", "order")
    show_full_result_count = False

    class Meta:
        """Meta class for the GroupTypeAdmin."""

        model = GroupType
