"""Admin settings for the MatchData model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import MatchData


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchDataAdminBase = ModelAdminBase[MatchData]
else:
    MatchDataAdminBase = admin.ModelAdmin


class MatchDataAdmin(MatchDataAdminBase):
    """Admin for the MatchData model."""

    list_display = [  # noqa: RUF012
        "id_uuid",
        "__str__",
        "home_score",
        "away_score",
        "part_length",
        "status",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchDataAdmin."""

        model = MatchData


admin.site.register(MatchData, MatchDataAdmin)
