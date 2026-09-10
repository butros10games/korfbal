"""Admin settings for the Timeout model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import Timeout
from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    TimeoutAdminBase = ModelAdminBase[Timeout]
else:
    TimeoutAdminBase = KorfbalModelAdmin


@admin.register(Timeout)
class TimeoutAdmin(TimeoutAdminBase):
    """Admin for the Timeout model."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "match_part__match_data__match_link__home_team",
        "match_part__match_data__match_link__away_team",
        "team__club",
        "pause__match_data__match_link__home_team",
        "pause__match_data__match_link__away_team",
    )

    list_display = ("match_data", "team", "match_part", "pause")
    list_filter = (
        relation_filter("team", "Team"),
        relation_filter("match_part", "Match part"),
    )
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "team__name",
    )
    autocomplete_fields = (
        "match_data",
        "match_part",
        "team",
        "pause",
    )
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = Timeout
