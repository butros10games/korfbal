"""Admin settings for the Match model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.schedule.models import Match


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchAdminBase = ModelAdminBase[Match]
else:
    MatchAdminBase = admin.ModelAdmin


class MatchAdmin(MatchAdminBase):
    """Admin settings for the Match model."""

    list_display = ["id_uuid", "home_team", "away_team", "start_time"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the Match model."""

        model = Match


admin.site.register(Match, MatchAdmin)
