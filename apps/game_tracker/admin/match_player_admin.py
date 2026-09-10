"""Admin settings for the MatchPlayer model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import MatchPlayer
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    MatchPlayerAdminBase = ModelAdminBase[MatchPlayer]
else:
    MatchPlayerAdminBase = KorfbalModelAdmin


@admin.register(MatchPlayer)
class MatchPlayerAdmin(MatchPlayerAdminBase):
    """Admin for the MatchPlayer model."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "team__club",
        "player__user",
    )
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
        "team__name",
        "player__name",
        "player__user__username",
    )

    list_display = ("match_data", "team", "player")
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchPlayerAdmin."""

        model = MatchPlayer
