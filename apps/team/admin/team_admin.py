"""Admin class for the Team model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.team.models import Team


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    TeamAdminBase = ModelAdminBase[Team]
else:
    TeamAdminBase = KorfbalModelAdmin


@admin.register(Team)
class TeamAdmin(TeamAdminBase):
    """Admin class for the Team model."""

    list_select_related = ("club",)
    ordering = ("club__name", "name")

    list_display = ("name", "club")
    search_fields = ("id_uuid", "name", "club__name")
    show_full_result_count = False

    class Meta:
        """Meta class for the Team model."""

        model = Team
