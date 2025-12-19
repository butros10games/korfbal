"""Admin settings for the PlayerMatchImpactBreakdown model."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import PlayerMatchImpactBreakdown


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerMatchImpactBreakdownAdminBase = ModelAdminBase[PlayerMatchImpactBreakdown]
else:
    PlayerMatchImpactBreakdownAdminBase = admin.ModelAdmin


class PlayerMatchImpactBreakdownAdmin(PlayerMatchImpactBreakdownAdminBase):
    """Admin for the PlayerMatchImpactBreakdown model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "impact",
        "algorithm_version",
        "computed_at",
    ]
    list_filter: ClassVar[list[str]] = ["algorithm_version"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "impact__match_data__id_uuid",
        "impact__player__user__username",
        "impact__player__user__email",
    ]
    autocomplete_fields: ClassVar[list[str]] = ["impact"]
    show_full_result_count = False


admin.site.register(PlayerMatchImpactBreakdown, PlayerMatchImpactBreakdownAdmin)
