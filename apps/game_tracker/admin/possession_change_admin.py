"""Admin settings for player-attributed possession changes."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PossessionChange


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PossessionChangeAdminBase = ModelAdminBase[PossessionChange]
else:
    PossessionChangeAdminBase = admin.ModelAdmin


@admin.register(PossessionChange)
class PossessionChangeAdmin(PossessionChangeAdminBase):
    """Expose possession changes for support and data review."""

    list_display = ("id_uuid", "match_data", "team", "player", "kind", "time")
    list_filter = ("kind", "team", "match_part")
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
