"""Admin settings for the PlayerMatchMinutes model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PlayerMatchMinutes


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerMatchMinutesAdminBase = ModelAdminBase[PlayerMatchMinutes]
else:
    PlayerMatchMinutesAdminBase = admin.ModelAdmin


class PlayerMatchMinutesAdmin(PlayerMatchMinutesAdminBase):
    """Admin for the PlayerMatchMinutes model."""

    list_display = (
        "id_uuid",
        "match_data",
        "player",
        "minutes_played",
        "algorithm_version",
        "computed_at",
    )
    list_filter = ("algorithm_version",)
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "player__user__username",
        "player__user__email",
    )
    autocomplete_fields = ("match_data", "player")
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PlayerMatchMinutes


admin.site.register(PlayerMatchMinutes, PlayerMatchMinutesAdmin)
