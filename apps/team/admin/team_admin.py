"""Admin class for the Team model."""

from django.contrib import admin

from apps.team.models import Team


class TeamAdmin(admin.ModelAdmin):
    """Admin class for the Team model."""

    list_display = ["id_uuid", "__str__", "club"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the Team model."""

        model = Team


admin.site.register(Team, TeamAdmin)
