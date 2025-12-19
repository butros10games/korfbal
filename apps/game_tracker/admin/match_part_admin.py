"""Admin settings for the MatchPart model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import MatchPart


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchPartAdminBase = ModelAdminBase[MatchPart]
else:
    MatchPartAdminBase = admin.ModelAdmin


class MatchPartAdmin(MatchPartAdminBase):
    """Admin for the MatchPart model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "start_time",
        "end_time",
        "match_data",
    ]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchPartAdmin."""

        model = MatchPart


admin.site.register(MatchPart, MatchPartAdmin)
