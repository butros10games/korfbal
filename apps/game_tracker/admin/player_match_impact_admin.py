"""Admin settings for the PlayerMatchImpact model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PlayerMatchImpact
from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerMatchImpactAdminBase = ModelAdminBase[PlayerMatchImpact]
else:
    PlayerMatchImpactAdminBase = KorfbalModelAdmin


@admin.register(PlayerMatchImpact)
class PlayerMatchImpactAdmin(PlayerMatchImpactAdminBase):
    """Admin for the PlayerMatchImpact model."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "player__user",
        "team__club",
    )

    list_display = (
        "match_data",
        "player",
        "team",
        "impact_score",
        "win_probability_added",
        "algorithm_version",
        "computed_at",
    )
    list_filter = ("algorithm_version", relation_filter("team", "Team"))
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "player__user__username",
        "player__user__email",
    )
    autocomplete_fields = ("match_data", "player", "team")
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PlayerMatchImpact
