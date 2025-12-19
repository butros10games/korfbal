"""Admin settings for the Match model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.schedule.models import Match


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchAdminBase = ModelAdminBase[Match]
else:
    MatchAdminBase = admin.ModelAdmin


class MatchAdmin(MatchAdminBase):
    """Admin settings for the Match model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "season",
        "home_team",
        "away_team",
        "start_time",
    ]
    list_filter: ClassVar[list[str]] = ["season", "start_time"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "home_team__name",
        "away_team__name",
    ]
    autocomplete_fields: ClassVar[list[str]] = ["season", "home_team", "away_team"]
    date_hierarchy = "start_time"
    show_full_result_count = False

    class Meta:
        """Meta class for the Match model."""

        model = Match


admin.site.register(Match, MatchAdmin)
