"""Admin settings for the PlayerMatchImpactBreakdown model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PlayerMatchImpactBreakdown
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerMatchImpactBreakdownAdminBase = ModelAdminBase[PlayerMatchImpactBreakdown]
else:
    PlayerMatchImpactBreakdownAdminBase = KorfbalModelAdmin


@admin.register(PlayerMatchImpactBreakdown)
class PlayerMatchImpactBreakdownAdmin(PlayerMatchImpactBreakdownAdminBase):
    """Admin for the PlayerMatchImpactBreakdown model."""

    list_select_related = (
        "impact__player__user",
        "impact__match_data__match_link__home_team",
        "impact__match_data__match_link__away_team",
    )

    list_display = ("impact", "algorithm_version", "computed_at")
    list_filter = ("algorithm_version",)
    search_fields = (
        "id_uuid",
        "impact__match_data__id_uuid",
        "impact__player__user__username",
        "impact__player__user__email",
    )
    autocomplete_fields = ("impact",)
    show_full_result_count = False
