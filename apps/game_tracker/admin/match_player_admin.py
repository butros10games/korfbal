"""Admin settings for the MatchPlayer model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import MatchPlayer


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchPlayerAdminBase = ModelAdminBase[MatchPlayer]
else:
    MatchPlayerAdminBase = admin.ModelAdmin


class MatchPlayerAdmin(MatchPlayerAdminBase):
    """Admin for the MatchPlayer model."""

    list_display = ["id_uuid", "match_data", "team", "player"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchPlayerAdmin."""

        model = MatchPlayer


admin.site.register(MatchPlayer, MatchPlayerAdmin)
