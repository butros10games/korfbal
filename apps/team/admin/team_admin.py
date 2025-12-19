"""Admin class for the Team model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.team.models import Team


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    TeamAdminBase = ModelAdminBase[Team]
else:
    TeamAdminBase = admin.ModelAdmin


class TeamAdmin(TeamAdminBase):
    """Admin class for the Team model."""

    list_display: ClassVar[list[str]] = ["id_uuid", "__str__", "club"]
    search_fields: ClassVar[list[str]] = ["id_uuid", "name", "club__name"]
    show_full_result_count = False

    class Meta:
        """Meta class for the Team model."""

        model = Team


admin.site.register(Team, TeamAdmin)
