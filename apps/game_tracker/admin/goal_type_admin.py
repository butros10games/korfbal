from django.contrib import admin

from ..models import GoalType


class GoalTypeAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False

    class Meta:
        model = GoalType


admin.site.register(GoalType, GoalTypeAdmin)
