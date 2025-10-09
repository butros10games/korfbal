"""Admin class for GoalType model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import GoalType


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    GoalTypeAdminBase = ModelAdminBase[GoalType]
else:
    GoalTypeAdminBase = admin.ModelAdmin


class GoalTypeAdmin(GoalTypeAdminBase):
    """Admin for the GoalType model."""

    list_display = ["id_uuid", "name"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the GoalTypeAdmin."""

        model = GoalType


admin.site.register(GoalType, GoalTypeAdmin)
