"""Admin settings for the PlayerChange model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import PlayerChange


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerChangeAdminBase = ModelAdminBase[PlayerChange]
else:
    PlayerChangeAdminBase = admin.ModelAdmin


class PlayerChangeAdmin(PlayerChangeAdminBase):
    """Admin for the PlayerChange model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "player_in",
        "player_out",
        "player_group",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the PlayerChangeAdmin."""

        model = PlayerChange


admin.site.register(PlayerChange, PlayerChangeAdmin)
