"""Admin settings for the PlayerMatchImpact model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import PlayerMatchImpact


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerMatchImpactAdminBase = ModelAdminBase[PlayerMatchImpact]
else:
    PlayerMatchImpactAdminBase = admin.ModelAdmin


class PlayerMatchImpactAdmin(PlayerMatchImpactAdminBase):
    """Admin for the PlayerMatchImpact model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "match_data",
        "player",
        "team",
        "impact_score",
        "algorithm_version",
        "computed_at",
    ]
    list_filter: ClassVar[list[str]] = ["algorithm_version", "team"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "match_data__id_uuid",
        "player__user__username",
        "player__user__email",
    ]
    autocomplete_fields: ClassVar[list[str]] = ["match_data", "player", "team"]
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PlayerMatchImpact


admin.site.register(PlayerMatchImpact, PlayerMatchImpactAdmin)
