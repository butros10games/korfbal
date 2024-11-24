"""Admin class for the Team model."""

from django.contrib import admin

from ..models import Team


class TeamAdmin(admin.ModelAdmin):
    """Admin class for the Team model."""

    list_display = ["id_uuid", "__str__", "club"]
    show_full_result_count = False

    class Meta:
        """Meta class for the Team model."""

        model = Team


admin.site.register(Team, TeamAdmin)
