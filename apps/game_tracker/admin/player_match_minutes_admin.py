"""Admin settings for the PlayerMatchMinutes model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import PlayerMatchMinutes


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerMatchMinutesAdminBase = ModelAdminBase[PlayerMatchMinutes]
else:
    PlayerMatchMinutesAdminBase = admin.ModelAdmin


class PlayerMatchMinutesAdmin(PlayerMatchMinutesAdminBase):
    """Admin for the PlayerMatchMinutes model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "match_data",
        "player",
        "minutes_played",
        "algorithm_version",
        "computed_at",
    ]
    list_filter: ClassVar[list[str]] = ["algorithm_version"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "match_data__id_uuid",
        "player__user__username",
        "player__user__email",
    ]
    autocomplete_fields: ClassVar[list[str]] = ["match_data", "player"]
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PlayerMatchMinutes


admin.site.register(PlayerMatchMinutes, PlayerMatchMinutesAdmin)
