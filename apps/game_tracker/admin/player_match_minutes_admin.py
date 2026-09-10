"""Admin settings for the PlayerMatchMinutes model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import PlayerMatchMinutes
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerMatchMinutesAdminBase = ModelAdminBase[PlayerMatchMinutes]
else:
    PlayerMatchMinutesAdminBase = KorfbalModelAdmin


@admin.register(PlayerMatchMinutes)
class PlayerMatchMinutesAdmin(PlayerMatchMinutesAdminBase):
    """Admin for the PlayerMatchMinutes model."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "player__user",
    )

    list_display = (
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
