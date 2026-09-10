"""Admin settings for the Pause model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import Pause
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PauseAdminBase = ModelAdminBase[Pause]
else:
    PauseAdminBase = KorfbalModelAdmin


@admin.register(Pause)
class PauseAdmin(PauseAdminBase):
    """Admin for the Pause model."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "match_part__match_data__match_link__home_team",
        "match_part__match_data__match_link__away_team",
    )
    list_filter = ("active", "start_time")
    ordering = ("-start_time",)

    list_display = ("match_data", "start_time", "end_time", "active")
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
    )
    show_full_result_count = False

    class Meta:
        """Meta class for the PauseAdmin."""

        model = Pause
