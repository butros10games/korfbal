"""Admin settings for the MatchPart model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import MatchPart
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    MatchPartAdminBase = ModelAdminBase[MatchPart]
else:
    MatchPartAdminBase = KorfbalModelAdmin


@admin.register(MatchPart)
class MatchPartAdmin(MatchPartAdminBase):
    """Admin for the MatchPart model."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
    )
    list_filter = ("active",)
    ordering = ("-start_time",)

    list_display = ("match_data", "part_number", "start_time", "end_time", "active")
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
    )
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchPartAdmin."""

        model = MatchPart
