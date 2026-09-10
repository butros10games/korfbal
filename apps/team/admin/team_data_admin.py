"""Admin class for the TeamData model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.team.models import TeamData


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    TeamDataAdminBase = ModelAdminBase[TeamData]
else:
    TeamDataAdminBase = KorfbalModelAdmin


@admin.register(TeamData)
class TeamDataAdmin(TeamDataAdminBase):
    """Admin class for the TeamData model."""

    list_select_related = ("team__club", "season")
    search_fields = ("team__id_uuid", "team__name", "team__club__name", "season__name")
    list_filter = ("season", "wedstrijd_sport")
    ordering = ("-season__start_date", "team__name")

    list_display = ["team", "season", "wedstrijd_sport", "team_rank"]
    show_full_result_count = False

    class Meta:
        """Meta class for the TeamData model."""

        model = TeamData
