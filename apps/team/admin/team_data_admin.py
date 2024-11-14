from django.contrib import admin

from ..models import TeamData


class TeamDataAdmin(admin.ModelAdmin):
    list_display = ["team", "season"]
    show_full_result_count = False

    class Meta:
        model = TeamData


admin.site.register(TeamData, TeamDataAdmin)
