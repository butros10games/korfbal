"""Admin settings for the MatchData model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import MatchData


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchDataAdminBase = ModelAdminBase[MatchData]
else:
    MatchDataAdminBase = admin.ModelAdmin


class MatchDataAdmin(MatchDataAdminBase):
    """Admin for the MatchData model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "__str__",
        "home_score",
        "away_score",
        "part_length",
        "status",
    ]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "match_link__id_uuid",
        "match_link__home_team__name",
        "match_link__away_team__name",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchDataAdmin."""

        model = MatchData


admin.site.register(MatchData, MatchDataAdmin)
