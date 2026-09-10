"""Admin class for the PlayerGroup model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PlayerGroup
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerGroupAdminBase = ModelAdminBase[PlayerGroup]
else:
    PlayerGroupAdminBase = KorfbalModelAdmin


@admin.register(PlayerGroup)
class PlayerGroupAdmin(PlayerGroupAdminBase):
    """Admin for the PlayerGroup model."""

    list_select_related = (
        "team__club",
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "starting_type",
        "current_type",
    )
    search_fields = (
        "id_uuid",
        "team__name",
        "team__club__name",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
    )
    list_filter = ("starting_type", "current_type")

    list_display = ("team", "match_data", "starting_type", "current_type")
    show_full_result_count = False

    class Meta:
        """Meta class for the PlayerGroupAdmin."""

        model = PlayerGroup
