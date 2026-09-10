"""Admin settings for the PlayerChange model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PlayerChange
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerChangeAdminBase = ModelAdminBase[PlayerChange]
else:
    PlayerChangeAdminBase = KorfbalModelAdmin


@admin.register(PlayerChange)
class PlayerChangeAdmin(PlayerChangeAdminBase):
    """Admin for the PlayerChange model."""

    list_select_related = (
        "player_in__user",
        "player_out__user",
        "player_group__team__club",
        "player_group__match_data__match_link__home_team",
        "player_group__match_data__match_link__away_team",
        "player_group__starting_type",
        "player_group__current_type",
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "match_part__match_data__match_link__home_team",
        "match_part__match_data__match_link__away_team",
    )
    search_fields = (
        "id_uuid",
        "player_in__name",
        "player_out__name",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
    )
    list_filter = ("time",)
    ordering = ("-time",)

    list_display = ("time", "match_data", "player_in", "player_out", "player_group")
    show_full_result_count = False

    class Meta:
        """Meta class for the PlayerChangeAdmin."""

        model = PlayerChange
