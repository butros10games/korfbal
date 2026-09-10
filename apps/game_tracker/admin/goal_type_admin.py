"""Admin class for GoalType model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import GoalType
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    GoalTypeAdminBase = ModelAdminBase[GoalType]
else:
    GoalTypeAdminBase = KorfbalModelAdmin


@admin.register(GoalType)
class GoalTypeAdmin(GoalTypeAdminBase):
    """Admin for the GoalType model."""

    search_fields = ("id_uuid", "name")
    ordering = ("name",)

    list_display = ("name",)
    show_full_result_count = False

    class Meta:
        """Meta class for the GoalTypeAdmin."""

        model = GoalType
