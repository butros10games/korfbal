"""Admin class for the TeamData model."""

from django.contrib import admin

from apps.team.models import TeamData


class TeamDataAdmin(admin.ModelAdmin):
    """Admin class for the TeamData model."""

    list_display = ["team", "season"]
    show_full_result_count = False

    class Meta:
        """Meta class for the TeamData model."""

        model = TeamData


admin.site.register(TeamData, TeamDataAdmin)
