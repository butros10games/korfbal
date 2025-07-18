"""Admin class for GoalType model."""

from django.contrib import admin

from apps.game_tracker.models import GoalType


class GoalTypeAdmin(admin.ModelAdmin):
    """Admin for the GoalType model."""

    list_display = ["id_uuid", "name"]
    show_full_result_count = False

    class Meta:
        """Meta class for the GoalTypeAdmin."""

        model = GoalType


admin.site.register(GoalType, GoalTypeAdmin)
