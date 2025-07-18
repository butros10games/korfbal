"""Admin settings for the GroupType model."""

from django.contrib import admin

from apps.game_tracker.models import GroupType


class GroupTypeAdmin(admin.ModelAdmin):
    """Admin for the GroupType model."""

    list_display = ["id_uuid", "name"]
    show_full_result_count = False

    class Meta:
        """Meta class for the GroupTypeAdmin."""

        model = GroupType


admin.site.register(GroupType, GroupTypeAdmin)
