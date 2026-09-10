"""Admin settings for player-attributed possession changes."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PossessionChange
from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PossessionChangeAdminBase = ModelAdminBase[PossessionChange]
else:
    PossessionChangeAdminBase = KorfbalModelAdmin


@admin.register(PossessionChange)
class PossessionChangeAdmin(PossessionChangeAdminBase):
    """Expose possession changes for support and data review."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "match_part__match_data__match_link__home_team",
        "match_part__match_data__match_link__away_team",
        "team__club",
        "player__user",
    )
    ordering = ("-time",)

    list_display = ("match_data", "team", "player", "kind", "time")
    list_filter = (
        "kind",
        relation_filter("team", "Team"),
        relation_filter("match_part", "Match part"),
    )
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "player__user__username",
        "team__name",
    )
    autocomplete_fields = ("match_data", "match_part", "team", "player")
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PossessionChange
