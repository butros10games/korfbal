"""Admin settings for the Match model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.schedule.models import Match


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    MatchAdminBase = ModelAdminBase[Match]
else:
    MatchAdminBase = KorfbalModelAdmin


@admin.register(Match)
class MatchAdmin(MatchAdminBase):
    """Admin settings for the Match model."""

    list_select_related = (
        "season",
        "home_team__club",
        "away_team__club",
        "pool__season",
    )
    ordering = ("-start_time",)

    list_display = ("start_time", "home_team", "away_team", "season", "pool")
    list_filter = ("season", "start_time")
    search_fields = (
        "id_uuid",
        "home_team__name",
        "away_team__name",
    )
    autocomplete_fields = ("season", "home_team", "away_team")
    date_hierarchy = "start_time"
    show_full_result_count = False

    class Meta:
        """Meta class for the Match model."""

        model = Match
