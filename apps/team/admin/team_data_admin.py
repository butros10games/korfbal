"""Admin class for the TeamData model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.team.models import TeamData


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    TeamDataAdminBase = ModelAdminBase[TeamData]
else:
    TeamDataAdminBase = admin.ModelAdmin


class TeamDataAdmin(TeamDataAdminBase):
    """Admin class for the TeamData model."""

    list_display = ["team", "season", "wedstrijd_sport", "team_rank"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the TeamData model."""

        model = TeamData


admin.site.register(TeamData, TeamDataAdmin)
